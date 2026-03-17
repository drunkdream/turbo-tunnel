# -*- coding: utf-8 -*-
"""Tunnel Server"""

import asyncio
from typing import List, Optional, Tuple, Type, Union

import tornado.tcpserver
import tornado.iostream

from . import chain
from . import registry
from . import route
from . import tunnel
from . import utils


class TunnelServer(object):
    """Tunnel Server"""

    retry_count: int = 0

    def __new__(
        cls,
        listen_url: Union[str, utils.Url],
        tunnel_router_or_urls: Union[route.TunnelRouter, List[Union[str, utils.Url]]],
    ) -> "TunnelServer":
        listen_url = (
            utils.Url(listen_url) if isinstance(listen_url, str) else listen_url
        )
        tunnel_router: Optional[route.TunnelRouter] = None
        tunnel_urls: List[utils.Url] = []
        if isinstance(tunnel_router_or_urls, route.TunnelRouter):
            tunnel_router = tunnel_router_or_urls
        else:
            tunnel_urls = [
                utils.Url(url) if isinstance(url, str) else url
                for url in tunnel_router_or_urls
            ]
        server_class: Optional[Type["TunnelServer"]] = registry.server_registry[
            listen_url.protocol
        ]
        if not server_class:
            raise RuntimeError(
                "%s tunnel server not registered" % listen_url.protocol.upper()
            )
        for tun in tunnel_urls:
            if not registry.tunnel_registry[tun.protocol]:
                raise RuntimeError("%s tunnel not registered" % tun.protocol.upper())
        instance = object.__new__(server_class)
        instance.__init__(listen_url, tunnel_router, tunnel_urls, True)
        return instance

    def __init__(
        self,
        listen_url: utils.Url,
        tunnel_router: Optional[route.TunnelRouter] = None,
        tunnel_urls: Optional[List[utils.Url]] = None,
        real_init: bool = False,
    ) -> None:
        if not real_init:
            return
        self._listen_url: utils.Url = listen_url
        self._tunnel_router: Optional[route.TunnelRouter] = tunnel_router
        self._tunnel_urls: Optional[List[utils.Url]] = tunnel_urls
        self._running: bool = True
        self.post_init()

    @property
    def final_tunnel(self) -> Optional[utils.Url]:
        if self._tunnel_urls:
            for tunnel_url in self._tunnel_urls[::-1]:
                if not tunnel_url.host or not tunnel_url.port:
                    continue
                return tunnel_url

        return None

    def post_init(self) -> None:
        pass

    def close(self) -> None:
        self._running = False

    def create_tunnel_chain(self) -> chain.TunnelChain:
        return chain.TunnelChain(
            self._tunnel_router or self._tunnel_urls, self.retry_count + 1
        )

    async def forward_data_to_upstream(
        self,
        tun_conn: "TunnelConnection",
        downstream: utils.IStream,
        upstream: utils.IStream,
    ) -> None:
        while self._running:
            try:
                buffer: bytes = await downstream.read()
            except utils.TunnelClosedError:
                tun_conn.on_downstream_closed()
                upstream.close()
                break

            try:
                await upstream.write(buffer)
            except utils.TunnelClosedError:
                tun_conn.on_upstream_closed()
                downstream.close()
                break
            else:
                tun_conn.on_data_sent(buffer)

    async def forward_data_to_downstream(
        self,
        tun_conn: "TunnelConnection",
        downstream: utils.IStream,
        upstream: utils.IStream,
    ) -> None:
        while self._running:
            try:
                buffer: bytes = await upstream.read()
            except utils.TunnelClosedError:
                tun_conn.on_upstream_closed()
                downstream.close()
                break
            else:
                tun_conn.on_data_recevied(buffer)

            try:
                await downstream.write(buffer)
            except utils.TunnelClosedError:
                tun_conn.on_downstream_closed()
                upstream.close()
                break

    def start(self) -> None:
        raise NotImplementedError


class TunnelConnection(object):
    """Tunnel Connection"""

    def __init__(
        self,
        client_address: Tuple[str, int],
        target_address: Tuple[str, int],
        tunnel_address: Optional[Tuple[str, int]] = None,
    ) -> None:
        self._client_address: Tuple[str, int] = client_address
        self._target_address: Tuple[str, int] = target_address
        self._tunnel_address: Optional[Tuple[str, int]] = tunnel_address
        self._bytes_sent: int = 0
        self._bytes_received: int = 0

    def __str__(self) -> str:  # pyright: ignore[reportImplicitOverride]
        tunnel_address = "Direct"
        if self._tunnel_address:
            tunnel_address = "%s:%d" % self._tunnel_address
        return "<%s object at 0x%x %s:%d -> %s -> %s:%d>" % (
            self.__class__.__name__,
            id(self),
            self._client_address[0],
            self._client_address[1],
            tunnel_address,
            self._target_address[0],
            self._target_address[1],
        )

    def __enter__(self) -> "TunnelConnection":
        self.on_open()
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        exc_trackback: Optional[object],
    ) -> None:
        self.on_close()

    @property
    def client_address(self) -> Tuple[str, int]:
        return self._client_address

    @property
    def target_address(self) -> Tuple[str, int]:
        return self._target_address

    @property
    def tunnel_address(self) -> Optional[Tuple[str, int]]:
        return self._tunnel_address

    def update_tunnel_address(self, tunnel_address: Tuple[str, int]) -> None:
        self._tunnel_address = tunnel_address
        registry.plugin_registry.notify("tunnel_address_updated", self, tunnel_address)

    def on_open(self) -> None:
        message = "[%s] New connection from %s:%d" % (
            self.__class__.__name__,
            self._client_address[0],
            self._client_address[1],
        )
        message += ", tunnel to %s:%d" % self._target_address
        if self._tunnel_address:
            message += " through %s:%d" % self._tunnel_address
        utils.logger.info(message)
        registry.plugin_registry.notify("new_connection", self)

    def on_data_recevied(self, buffer: bytes) -> None:
        self._bytes_received += len(buffer)
        utils.logger.debug(
            "[%s][%s:%d][%s:%d] %d bytes received"
            % (
                self.__class__.__name__,
                self._client_address[0],
                self._client_address[1],
                self._target_address[0],
                self._target_address[1],
                len(buffer),
            )
        )
        registry.plugin_registry.notify("data_recevied", self, buffer)

    def on_data_sent(self, buffer: bytes) -> None:
        self._bytes_sent += len(buffer)
        utils.logger.debug(
            "[%s][%s:%d][%s:%d] %d bytes sent"
            % (
                self.__class__.__name__,
                self._client_address[0],
                self._client_address[1],
                self._target_address[0],
                self._target_address[1],
                len(buffer),
            )
        )
        registry.plugin_registry.notify("data_sent", self, buffer)

    def on_upstream_closed(self) -> None:
        utils.logger.info(
            "[%s][%s:%d][%s:%d] Upstream closed"
            % (
                self.__class__.__name__,
                self._client_address[0],
                self._client_address[1],
                self._target_address[0],
                self._target_address[1],
            )
        )

    def on_downstream_closed(self) -> None:
        utils.logger.info(
            "[%s][%s:%d][%s:%d] Downstream closed"
            % (
                self.__class__.__name__,
                self._client_address[0],
                self._client_address[1],
                self._target_address[0],
                self._target_address[1],
            )
        )

    def on_close(self) -> None:
        utils.logger.debug(
            "[%s][%s:%d][%s:%d] Connection closed, total %d bytes sent, %d bytes received"
            % (
                self.__class__.__name__,
                self._client_address[0],
                self._client_address[1],
                self._target_address[0],
                self._target_address[1],
                self._bytes_sent,
                self._bytes_received,
            )
        )
        registry.plugin_registry.notify("connection_closed", self)


class TCPTunnelServer(
    TunnelServer, tornado.tcpserver.TCPServer
):  # pyright: ignore[reportUnsafeMultipleInheritance]
    """TCP Tunnel Server"""

    def post_init(self) -> None:  # pyright: ignore[reportImplicitOverride]
        tornado.tcpserver.TCPServer.__init__(self)

    @property
    def final_tunnel(
        self,
    ) -> Optional[utils.Url]:  # pyright: ignore[reportImplicitOverride]
        if self._tunnel_urls:
            for tunnel_url in self._tunnel_urls[:-1][::-1]:
                if not tunnel_url.host or not tunnel_url.port:
                    continue
                return tunnel_url
        return None

    async def handle_stream(  # pyright: ignore[reportImplicitOverride]
        self, stream: tornado.iostream.IOStream, address: Tuple[str, int]
    ) -> None:
        if not self._tunnel_urls:
            stream.close()
            return

        last_tunnel = self._tunnel_urls[-1]
        if not last_tunnel.host or not last_tunnel.port:
            stream.close()
            return

        target_address: Tuple[str, int] = (last_tunnel.host, last_tunnel.port)
        downstream = tunnel.TCPTunnel(stream)

        # Get tunnel address safely
        final_tun = self.final_tunnel
        tunnel_addr: Optional[Tuple[str, int]] = None
        if final_tun and final_tun.host and final_tun.port:
            tunnel_addr = (final_tun.host, final_tun.port)

        with TunnelConnection(address, target_address, tunnel_addr) as tun_conn:
            with self.create_tunnel_chain() as tunnel_chain:
                try:
                    await tunnel_chain.create_tunnel(target_address)
                except utils.TunnelError as e:
                    utils.logger.warn(
                        "[%s] Connect %s:%d failed: %s"
                        % (
                            self.__class__.__name__,
                            target_address[0],
                            target_address[1],
                            e,
                        )
                    )
                    stream.close()
                    return

                if not tunnel_chain.tail:
                    stream.close()
                    return

                tasks = [
                    utils.AsyncTaskManager().wrap_task(
                        self.forward_data_to_upstream(
                            tun_conn, downstream, tunnel_chain.tail
                        )
                    ),
                    utils.AsyncTaskManager().wrap_task(
                        self.forward_data_to_downstream(
                            tun_conn, downstream, tunnel_chain.tail
                        )
                    ),
                ]
                _ = await utils.wait_for_tasks(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                downstream.close()

    def start(self) -> None:
        """Start TCP server - implements TunnelServer.start()"""
        self.listen(self._listen_url.port or 0, self._listen_url.host)
        utils.logger.info(
            "[%s] TCP server is listening on %s:%d"
            % (
                self.__class__.__name__,
                self._listen_url.host,
                self._listen_url.port or 0,
            )
        )


registry.server_registry.register("tcp", TCPTunnelServer)
