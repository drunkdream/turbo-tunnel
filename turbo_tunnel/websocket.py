# -*- coding: utf-8 -*-

'''WebSocket Tunnel
'''

import socket
import time

import tornado.websocket

from . import registry
from . import tunnel
from . import utils

class WebSocketTunnelConnection(tornado.websocket.WebSocketClientConnection):
    '''WebSocket Client Support using exist connection 
    '''

    def __init__(self, stream, url, headers=None, timeout=15):
        self._stream = stream
        self._url = url
        self._connected = None
        self._closed = False
        self.__timeout = timeout
        compression_options = None
        request = tornado.httpclient.HTTPRequest(self._url, headers=headers, connect_timeout=timeout, request_timeout=timeout)
        request = tornado.httpclient._RequestProxy(request, tornado.httpclient.HTTPRequest._DEFAULTS)
        super(WebSocketTunnelConnection, self).__init__(request,
            on_message_callback=self.on_message,
            compression_options=compression_options
        )
        self._patcher = self._patch_tcp_client(self._stream)
        self._patcher.patch()
        self._buffer = b''

    def _patch_tcp_client(self, stream):
        TCPClient = tornado.tcpclient.TCPClient
        
        async def connect(
            tcp_client,
            host,
            port,
            af=socket.AF_UNSPEC,
            ssl_options=None,
            max_buffer_size=None,
            source_ip=None,
            source_port=None,
            timeout=None):
            return stream
    
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
        await super(WebSocketTunnelConnection, self).headers_received(start_line, headers)
        if start_line.code != 101:
            utils.logger.error('[%s] Connect %s return %d' % (self.__class__.__name__, self._url, start_line.code))
            self._connected = False
        else:
            self._connected = True

    def on_message(self, message):
        if not message:
            self._closed = True
        else:
            self._buffer += message

    async def wait_for_connecting(self):
        time0 = time.time()
        while time.time() - time0 < self.__timeout:
            if self._connected == None:
                await tornado.gen.sleep(0.01)
                continue
            self._patcher.unpatch()
            return self._connected
        else:
            utils.logger.warn('[%s] Connect %s timeout' % (self.__class__.__name__, self._url))
            self._patcher.unpatch()
            return False

    async def read(self):
        if self._closed:
            raise utils.TunnelClosedError()

        while not self._buffer:
            await tornado.gen.sleep(0.005)
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
        url.path = url.path.replace('{addr}', address[0])
        url.path = url.path.replace('{port}', str(address[1]))
        super(WebSocketTunnel, self).__init__(tunnel, url, address)
        self._upstream = None

    async def connect(self):
        self._upstream = WebSocketTunnelConnection(self._tunnel.stream, str(self._url))
        return await self._upstream.wait_for_connecting()

    async def read(self):
        return await self._upstream.read()

    async def write(self, buffer):
        return await self._upstream.write(buffer)

    def close(self):
        if self._upstream:
            self._upstream.close()
            self._upstream = None

registry.tunnel_registry.register('ws', WebSocketTunnel)
    
