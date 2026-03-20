# -*- coding: utf-8 -*-
"""HTTPS Tunnel"""

import re
from typing import Any, Dict, List, Optional, Tuple, Union

import tornado.httpserver
import tornado.httpclient
import tornado.iostream
import tornado.routing
import tornado.simple_httpclient
import tornado.web

from . import auth
from . import registry
from . import server
from . import tunnel
from . import utils


class HTTPSTunnel(tunnel.TCPTunnel):
    """HTTPS Tunnel"""

    @property
    def socket(self) -> Any:
        if self._tunnel:
            return self._tunnel.socket
        return None

    @property
    def stream(self) -> Any:
        if self._tunnel:
            return self._tunnel.stream
        return None

    async def connect(self) -> bool:
        if not self._tunnel:
            return False

        # Type narrowing: after the check above, _tunnel is not None
        tunnel = self._tunnel
        data = (
            "CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\nProxy-Connection: Keep-Alive\r\n"
            % (
                self._addr,
                self._port,
                self._addr,
                self._port,
            )
        )
        auth_data = self._url.auth
        if auth_data:
            data += "Proxy-Authorization: Basic %s\r\n" % auth.http_basic_auth(
                *auth_data.split(":")
            )
        data += "\r\n"
        await tunnel.write(data.encode())
        buffer = bytearray()
        while True:
            buffer += await tunnel.read()
            if buffer.endswith(b"\r\n\r\n"):
                break

        lines: List[bytes] = buffer.strip().split(b"\r\n")
        items: List[bytes] = lines[0].split()
        code: int = int(items[1])
        reason: str = (b" ".join(items[2:])).decode()
        if code == 200:
            return True
        utils.logger.warning(
            "[%s] Connect %s:%d over %s failed: [%d] %s"
            % (self.__class__.__name__, self._addr, self._port, self._url, code, reason)
        )
        return False

    def close(self) -> None:
        if self._tunnel:
            self._tunnel.close()
            self._tunnel = None


class DefaultHandler(tornado.web.RequestHandler):
    pass


class HTTPRouter(tornado.routing.Router):
    """Support CONNECT method"""

    def __init__(
        self,
        app: tornado.web.Application,
        handlers: Optional[List[Tuple[List[str], str, type]]] = None,
    ) -> None:
        super().__init__()
        self._app: tornado.web.Application = app
        self._handlers: List[Tuple[List[str], str, type]] = handlers or []

    def find_handler(
        self, request: Any, **kwargs: Any  # tornado.httputil.HTTPServerRequest
    ) -> Any:  # tornado.httputil.HTTPMessageDelegate
        handler: type = DefaultHandler
        if request.method == "CONNECT":
            for methods, _, _handler in self._handlers:
                if "CONNECT" in methods:
                    handler = _handler
                    break
        else:
            for methods, pattern, _handler in self._handlers:
                if request.method not in methods and "*" not in methods:
                    continue
                if not re.match(pattern, request.path):
                    continue
                handler = _handler
                break

        return self._app.get_handler_delegate(
            request, handler
        )  # , path_args=[request.path]


class HTTPSTunnelServer(server.TunnelServer):
    """HTTPS Tunnel Server"""

    _tunnels: Dict[Tuple[str, int], List[Dict[str, Any]]]
    _http_server: tornado.httpserver.HTTPServer

    def post_init(self) -> None:
        this = self
        self._tunnels = {}

        class EnumHTTPTunnelStatus(object):
            IDLE: int = 1
            BUSY: int = 2

        class HTTPServerHandler(tornado.web.RequestHandler):
            """HTTP Server Handler"""

            SUPPORTED_METHODS: List[str] = list(
                tornado.web.RequestHandler.SUPPORTED_METHODS
            ) + ["CONNECT"]

            async def _get_tunnel(self, address: Tuple[str, int]) -> Dict[str, Any]:
                if address not in this._tunnels:
                    this._tunnels[address] = []
                force_instance = False
                tunnel_chain = this.create_tunnel_chain()
                tunnel_urls = await tunnel_chain.select_tunnel(address)
                if tunnel_urls and tunnel_urls[-1].protocol == "http":
                    tunnel_urls[-1].protocol = "tcp"  # request as http proxy
                    force_instance = True

                if not force_instance:
                    for i in range(len(this._tunnels[address]) - 1, -1, -1):
                        tunn = this._tunnels[address][i]
                        if tunn["tunnel"].closed():
                            utils.logger.info(
                                "[%s] HTTP tunnel %s closed"
                                % (self.__class__.__name__, tunn["tunnel"])
                            )
                            this._tunnels[address].pop(i)
                            continue
                        if tunn["status"] != EnumHTTPTunnelStatus.IDLE:
                            utils.logger.debug(
                                "[%s] HTTP tunnel %s is busy"
                                % (self.__class__.__name__, tunn["tunnel"])
                            )
                            continue
                        utils.logger.info(
                            "[%s] Use cached tunnel %s to access %s:%d"
                            % (
                                self.__class__.__name__,
                                this._tunnels[address][i]["tunnel"],
                                address[0],
                                address[1],
                            )
                        )
                        return this._tunnels[address][i]

                await tunnel_chain.create_tunnel(address, tunnel_urls)
                tunn = {
                    "tunnel": tunnel_chain.tail,
                    "status": EnumHTTPTunnelStatus.IDLE,
                }
                this._tunnels[address].append(tunn)
                return tunn

            async def handle_request(self) -> None:
                s_url: str = self.request.path
                if self.request.query:
                    s_url += "?" + self.request.query
                utils.logger.debug(
                    "[%s][%s] %s"
                    % (
                        self.__class__.__name__,
                        (self.request.method or "").upper(),
                        s_url,
                    )
                )
                url = utils.Url(s_url)
                if url.protocol != "http" or not url.host or not url.port:
                    self.set_status(400)
                    return

                url_address: Tuple[str, int] = (url.host, url.port)
                try:
                    tunn = await self._get_tunnel(url_address)
                except utils.TunnelError as e:
                    if not isinstance(e, utils.TunnelBlockedError):
                        utils.logger.warning(
                            "[%s] Connect %s:%d failed: %s"
                            % (
                                self.__class__.__name__,
                                url.address[0],
                                url.address[1],
                                e,
                            )
                        )
                        self.set_status(504)
                    else:
                        self.set_status(403)
                    return

                this = self

                class _HTTPConnection(tornado.simple_httpclient._HTTPConnection):
                    async def headers_received(
                        self, first_line: Any, headers: Any
                    ) -> None:
                        this.set_status(first_line.code, first_line.reason)
                        this.set_header("Transfer-Encoding", "chunked")
                        for k, v in headers.get_all():
                            if k == "Content-Length":
                                continue
                            elif k in ("Set-Cookie",):
                                this.add_header(k, v)
                            else:
                                this.set_header(k, v)
                        this.set_header("Connection", "Close")
                        _ = this.flush()

                    def data_received(self, chunk: bytes) -> None:
                        chunk_data: bytes = b"%x\r\n%b\r\n" % (len(chunk), chunk)
                        this.write(chunk_data)
                        _ = this.flush()

                    def finish(self) -> None:
                        chunk: bytes = b"0\r\n\r\n"
                        this.write(chunk)
                        _ = this.flush()
                        # self._release()
                        # self._on_end_request()
                        tunn["status"] = EnumHTTPTunnelStatus.IDLE

                http_client = tornado.simple_httpclient.SimpleAsyncHTTPClient(
                    force_instance=True
                )
                http_client.max_body_size = 500 * 1024 * 1024  # type: ignore[attr-defined]
                http_client._connection_class = lambda: _HTTPConnection  # type: ignore[attr-defined]
                tunnel.patch_tcp_client(http_client.tcp_client, tunn["tunnel"])  # type: ignore[attr-defined]
                tunn["status"] = EnumHTTPTunnelStatus.BUSY

                headers: Dict[str, Any] = {}
                for hdr in self.request.headers:
                    if hdr == "Proxy-Connection":
                        if self.request.headers[hdr].lower() == "keep-alive":
                            headers["Connection"] = "Keep-Alive"
                    else:
                        headers[hdr] = self.request.headers[hdr]

                request = tornado.httpclient.HTTPRequest(
                    s_url,
                    self.request.method or "GET",
                    headers=headers,
                    body=self.request.body,
                    follow_redirects=False,
                    allow_nonstandard_methods=True,
                    request_timeout=300,
                )

                try:
                    _ = await http_client.fetch(request)
                except tornado.httpclient.HTTPClientError as e:
                    if tunn.get("tunnel"):
                        tunn["tunnel"].close()  # type: ignore[attr-defined]
                    if e.code == 599:
                        self.set_status(502)
                    elif e.response:
                        self.set_status(e.code, e.message)
                        for hdr in e.response.headers:
                            if hdr in ("Content-Length",):
                                continue
                            elif hdr == "Set-Cookie":
                                for it in e.response.headers.get_list(hdr):
                                    self.add_header(hdr, it)
                            elif hdr not in ("Transfer-Encoding",):
                                self.set_header(hdr, e.response.headers[hdr])
                        if e.response.body:
                            self.write(e.response.body)

            async def get(self) -> None:
                return await self.handle_request()

            async def head(self) -> None:
                return await self.handle_request()

            async def options(self) -> None:
                return await self.handle_request()

            async def patch(self) -> None:
                return await self.handle_request()

            async def post(self) -> None:
                return await self.handle_request()

            async def put(self) -> None:
                return await self.handle_request()

            async def connect(self) -> None:
                path: str = self.request.path
                address: Tuple[str, int]
                if path.startswith("::1"):
                    address = ("::1", int(path[4:]))
                else:
                    parts = path.split(":")
                    address = (parts[0], int(parts[1]))
                downstream = tunnel.TCPTunnel(
                    self.request.connection.detach(),  # type: ignore[union-attr]
                    server_side=True,
                )
                auth_data = this._listen_url.auth
                if auth_data:
                    auth_data = auth_data.split(":")
                    for header in self.request.headers:
                        if header == "Proxy-Authorization":
                            value = self.request.headers[header]
                            auth_type, auth_value = value.split()
                            if (
                                auth_type == "Basic"
                                and auth_value == auth.http_basic_auth(*auth_data)
                            ):
                                break
                    else:
                        utils.logger.info(
                            "[%s] Connection to %s:%d refused due to wrong auth"
                            % (self.__class__.__name__, address[0], address[1])
                        )
                        _ = await downstream.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                        self._finished = True
                        return

                # Get tunnel address safely
                final_tun = this.final_tunnel
                tunnel_addr: Optional[Tuple[str, int]] = None
                if final_tun and final_tun.host and final_tun.port:
                    tunnel_addr = (final_tun.host, final_tun.port)

                with server.TunnelConnection(
                    self.request.connection.context.address,  # type: ignore[union-attr]
                    address,
                    tunnel_address=tunnel_addr,
                ) as tun_conn:
                    with this.create_tunnel_chain() as tunnel_chain:
                        try:
                            await tunnel_chain.create_tunnel(address)
                        except utils.TunnelError as e:
                            if not isinstance(e, utils.TunnelBlockedError):
                                utils.logger.warning(
                                    "[%s] Connect %s:%d failed: %s"
                                    % (
                                        self.__class__.__name__,
                                        address[0],
                                        address[1],
                                        e,
                                    )
                                )
                            if not downstream.closed():
                                if isinstance(e, utils.TunnelBlockedError):
                                    _ = await downstream.write(
                                        b"HTTP/1.1 403 Forbidden\r\n\r\n"
                                    )
                                else:
                                    _ = await downstream.write(
                                        b"HTTP/1.1 504 Gateway timeout\r\n\r\n"
                                    )
                            else:
                                tun_conn.on_downstream_closed()
                            self._finished = True
                            return
                        else:
                            if tunnel_chain.tunnel_urls:
                                tunnel_url = tunnel_chain.tunnel_urls[-1]
                                if tunnel_url.host:
                                    tun_conn.update_tunnel_address(
                                        (tunnel_url.host, tunnel_url.port or 0)
                                    )

                            if not downstream.closed():
                                _ = await downstream.write(
                                    b"HTTP/1.1 200 HTTPSTunnel Established\r\n\r\n"
                                )
                                if not tunnel_chain.tail:
                                    return

                                tasks = [
                                    this.forward_data_to_upstream(
                                        tun_conn, downstream, tunnel_chain.tail
                                    ),
                                    this.forward_data_to_downstream(
                                        tun_conn, downstream, tunnel_chain.tail
                                    ),
                                ]
                                _ = await utils.AsyncTaskManager().wait_for_tasks(tasks)
                            else:
                                utils.logger.warning(
                                    "[%s] Downstream closed unexpectedly"
                                    % self.__class__.__name__
                                )
                                tun_conn.on_downstream_closed()
                        downstream.close()
                        self._finished = True

        handlers: List[Tuple[List[str], str, type]] = [
            (["CONNECT"], r"", HTTPServerHandler),
            (["*"], r".*", HTTPServerHandler),
        ]
        app = tornado.web.Application()
        router = HTTPRouter(app, handlers)
        self._http_server = tornado.httpserver.HTTPServer(router)

    def start(self) -> None:
        port: int = self._listen_url.port or 8080
        self._http_server.listen(port, self._listen_url.host)
        utils.logger.info(
            "[%s] HTTP server is listening on %s:%d"
            % (self.__class__.__name__, self._listen_url.host, port)
        )


registry.tunnel_registry.register("http", HTTPSTunnel)
registry.tunnel_registry.register("https", HTTPSTunnel)
registry.server_registry.register("http", HTTPSTunnelServer)
registry.server_registry.register("https", HTTPSTunnelServer)
