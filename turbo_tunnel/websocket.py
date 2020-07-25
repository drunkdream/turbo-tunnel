# -*- coding: utf-8 -*-
'''WebSocket Tunnel
'''

import asyncio
import copy
import re
import socket
import time

import tornado.httputil
import tornado.websocket

from . import auth
from . import registry
from . import server
from . import tunnel
from . import utils


class WebSocketTunnelConnection(tornado.websocket.WebSocketClientConnection):
    '''WebSocket Client Support using exist connection 
    '''
    def __init__(self, tunnel, url, headers=None, timeout=15):
        self._tunnel = tunnel
        self._url = url
        self._connected = None
        self._closed = False
        self.__timeout = timeout
        compression_options = None
        if isinstance(headers, dict):
            headers = tornado.httputil.HTTPHeaders(headers)
        request = tornado.httpclient.HTTPRequest(self._url,
                                                 headers=headers,
                                                 connect_timeout=timeout,
                                                 request_timeout=timeout)
        request = tornado.httpclient._RequestProxy(
            request, tornado.httpclient.HTTPRequest._DEFAULTS)
        super(WebSocketTunnelConnection,
              self).__init__(request,
                             on_message_callback=self.on_message,
                             compression_options=compression_options)
        self._patcher = self._patch_tcp_client(self._tunnel)
        self._patcher.patch()
        self._buffer = b''
        self._read_event = asyncio.Event()

    def _patch_tcp_client(self, tunn):
        TCPClient = tornado.tcpclient.TCPClient

        async def connect(tcp_client,
                          host,
                          port,
                          af=socket.AF_UNSPEC,
                          ssl_options=None,
                          max_buffer_size=None,
                          source_ip=None,
                          source_port=None,
                          timeout=None):
            return tunnel.TunnelIOStream(tunn)

        class TCPClientPatchContext(object):
            def __init__(self, patched_connect):
                self._origin_connect = TCPClient.connect
                self._patched_connect = patched_connect

            def patch(self):
                TCPClient.connect = self._patched_connect

            def unpatch(self):
                TCPClient.connect = self._origin_connect

            def __enter__(self):
                self.patch()

            def __exit__(self, exc_type, exc_value, exc_trackback):
                self.unpatch()

        return TCPClientPatchContext(connect)

    async def headers_received(self, start_line, headers):
        await super(WebSocketTunnelConnection,
                    self).headers_received(start_line, headers)
        if start_line.code != 101:
            utils.logger.error(
                '[%s] Connect %s return %d' %
                (self.__class__.__name__, self._url, start_line.code))
            self._connected = False
        else:
            self._connected = True

    def on_message(self, message):
        if not message:
            self._closed = True
        else:
            self._buffer += message
        self._read_event.set()

    async def wait_for_connecting(self):
        time0 = time.time()
        while time.time() - time0 < self.__timeout:
            if self._connected == None:
                await asyncio.sleep(0.005)
                continue
            self._patcher.unpatch()
            return self._connected
        else:
            utils.logger.warn('[%s] Connect %s timeout' %
                              (self.__class__.__name__, self._url))
            self._patcher.unpatch()
            return False

    async def read(self):
        while not self._buffer:
            if self._closed:
                raise utils.TunnelClosedError()
            await self._read_event.wait()
            self._read_event.clear()
        buffer = self._buffer
        self._buffer = b''
        return buffer

    async def write(self, buffer):
        try:
            await self.write_message(buffer, True)
        except tornado.websocket.WebSocketClosedError:
            raise utils.TunnelClosedError()
        return len(buffer)


class WebSocketTunnel(tunnel.Tunnel):
    '''WebSocket Tunnel
    '''
    def __init__(self, tunnel, url, address):
        url = copy.copy(url)
        url.path = url.path.format(addr=address[0], port=address[1])
        super(WebSocketTunnel, self).__init__(tunnel, url, address)
        self._upstream = None

    async def connect(self):
        headers = {}
        auth_data = self._url.auth
        if auth_data:
            headers['Proxy-Authorization'] = 'Basic %s' % auth.http_basic_auth(
                *auth_data.split(':'))
        self._upstream = WebSocketTunnelConnection(self._tunnel,
                                                   str(self._url), headers)
        return await self._upstream.wait_for_connecting()

    async def read(self):
        return await self._upstream.read()

    async def write(self, buffer):
        return await self._upstream.write(buffer)

    def close(self):
        if self._upstream:
            self._upstream.close()
            self._upstream = None


class WebSocketDownStream(utils.IStream):
    def __init__(self, handler):
        self._handler = handler
        self._buffer = b''
        self._read_event = asyncio.Event()

    def on_recv(self, buffer):
        self._buffer += buffer
        self._read_event.set()

    async def read(self):
        while not self._buffer:
            if not self._handler:
                raise utils.TunnelClosedError
            await self._read_event.wait()
            self._read_event.clear()
        buffer = self._buffer
        self._buffer = b''
        return buffer

    async def write(self, buffer):
        await self._handler.write_message(buffer, True)

    def close(self):
        if self._handler:
            self._read_event.set()
            self._handler.close()
            self._handler = None


class WebSocketTunnelServer(server.TunnelServer):
    '''WebSocket Tunnel Server
    '''
    def post_init(self):
        this = self

        class WebSocketProtocol(tornado.websocket.WebSocketProtocol13):
            async def accept_connection(self, handler):
                if await self.handler.connect():
                    await super(WebSocketProtocol,
                                self).accept_connection(handler)

        class WebSocketProxyHandler(tornado.websocket.WebSocketHandler):
            '''WebSocket Proxy Handler
            '''
            def __init__(self, *args, **kwargs):
                super(WebSocketProxyHandler, self).__init__(*args, **kwargs)
                self._tun_conn = None
                self._tunnel_chain = None
                self._downstream = None

            async def connect(self):
                '''connect target server
                '''
                address = None
                ret = re.match(this._listen_url.path, self.request.path)
                if ret:
                    addr = ret.groupdict().get('addr')
                    port = ret.groupdict().get('port')
                    if addr and port and port.isdigit():
                        address = addr, int(port)
                if not address:
                    self.set_status(404, "Not Found")
                    return False
                auth_data = this._listen_url.auth
                if auth_data:
                    auth_data = auth_data.split(':')
                    for header in self.request.headers:
                        if header == 'Proxy-Authorization':
                            value = self.request.headers[header]
                            auth_type, auth_value = value.split()
                            if auth_type == 'Basic' and auth_value == auth.http_basic_auth(
                                    *auth_data):
                                break
                    else:
                        utils.logger.info(
                            '[%s] Connection to %s:%d refused due to wrong auth'
                            %
                            (self.__class__.__name__, address[0], address[1]))
                        self.set_status(403, 'Forbidden')
                        return False

                self._tun_conn = server.TunnelConnection(
                    self.request.connection.context.address, address,
                    this.final_tunnel and this.final_tunnel.address)
                self._tun_conn.on_open()

                self._tunnel_chain = this.create_tunnel_chain()

                try:
                    await self._tunnel_chain.create_tunnel(address)
                except utils.TunnelError as e:
                    if not isinstance(e, utils.TunnelBlockedError):
                        utils.logger.warn('[%s] Connect %s:%d failed: %s' %
                                          (self.__class__.__name__, address[0],
                                           address[1], e))
                        self.set_status(504, 'Gateway timeout')
                    else:
                        self.set_status(403, 'Forbidden')
                    return False
                self._downstream = WebSocketDownStream(self)
                utils.AsyncTaskManager().start_task(
                    this.forward_data_to_upstream(self._tun_conn,
                                                  self._downstream,
                                                  self._tunnel_chain.tail))
                utils.AsyncTaskManager().start_task(
                    this.forward_data_to_downstream(self._tun_conn,
                                                    self._downstream,
                                                    self._tunnel_chain.tail))
                return True

            def get_websocket_protocol(self):
                '''Override to connect target server
                '''
                websocket_version = self.request.headers.get(
                    "Sec-WebSocket-Version")
                if websocket_version in ("7", "8", "13"):
                    params = tornado.websocket._WebSocketParams(
                        ping_interval=self.ping_interval,
                        ping_timeout=self.ping_timeout,
                        max_message_size=self.max_message_size,
                        compression_options=self.get_compression_options(),
                    )
                    return WebSocketProtocol(self,
                                             mask_outgoing=True,
                                             params=params)

            async def on_message(self, message):
                self._downstream.on_recv(message)

            def on_connection_close(self):
                super(WebSocketProxyHandler, self).on_connection_close()
                self._downstream.close()
                self._tun_conn.on_downstream_closed()
                self._tun_conn.on_close()
                self._tun_conn = None
                self._tunnel_chain.close()
                self._tunnel_chain = None

        path = self._listen_url.path.format(addr=r'(?P<addr>[\w\.]+)',
                                            port=r'(?P<port>\d+)')
        self._listen_url.path = path
        handlers = [
            (path, WebSocketProxyHandler),
        ]
        self._app = tornado.web.Application(handlers)

    def start(self):
        self._app.listen(self._listen_url.port, self._listen_url.host)
        utils.logger.info('[%s] WebSocket server is listening on %s:%d' %
                          (self.__class__.__name__, self._listen_url.host,
                           self._listen_url.port))


registry.tunnel_registry.register('ws', WebSocketTunnel)
registry.server_registry.register('ws', WebSocketTunnelServer)
