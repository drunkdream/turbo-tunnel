# -*- coding: utf-8 -*-

'''Miscellaneous utility functions and classes
'''

import logging
import socket
import urllib.parse

import tornado.iostream

logger = logging.getLogger('turbo-tunnel')


class Url(object):
    def __init__(self, url):
        self._url = url
        obj = urllib.parse.urlparse(url)
        self._protocol = obj.scheme
        netloc = obj.netloc
        auth = ''
        port = 0
        if '@' in netloc:
            auth, netloc = netloc.split('@')
        if ':' in netloc:
            netloc, port = netloc.split(':')
        self._auth, self._host, self._port = auth, netloc, int(port)
        if not self._host and self._port:
            self._host = '0.0.0.0'
        self._path = obj.path

    def __str__(self):
        port = self.port or ''
        if self._protocol in ('http', 'ws') and port == 80:
            port = ''
        elif self._protocol in ('https', 'wss') and port == 443:
            port = ''
        elif port:
            port = ':%d' % port
        return '%s://%s%s%s' % (self._protocol, self._host, port, self._path)

    @property
    def protocol(self):
        return self._protocol

    @property
    def host(self):
        return self._host

    @property
    def port(self):
        if self._port:
            return self._port
        if self.protocol in ('http', 'ws'):
            return 80
        elif self.protocol in ('https', 'wss'):
            return 443

    @property
    def address(self):
        return self.host, self.port

    @property
    def path(self):
        return self._path

    @path.setter
    def path(self, value):
        self._path = value


class TCPStream(object):
    '''IOStream Wrapper
    '''
    def __init__(self, socket_or_stream):
        if isinstance(socket_or_stream, socket.socket):
            self._stream = tornado.iostream.IOStream(socket_or_stream)
        else:
            self._stream = socket_or_stream

    def __getattr__(self, attr):
        return getattr(self._stream, attr)

    @property
    def socket(self):
        return self._stream.socket

    @property
    def stream(self):
        return self._stream

    def close(self):
        if self._stream:
            self._stream.close()
            self._stream = None

    async def read(self):
        try:
            buffer = await self._stream.read_bytes(4096, partial=True)
        except tornado.iostream.StreamClosedError:
            pass
        else:
            if buffer:
                return buffer
        raise TunnelClosedError()

    async def write(self, buffer):
        await self._stream.write(buffer)


class TimeoutError(RuntimeError):
    pass


class TunnelError(RuntimeError):
    pass


class TunnelConnectError(TunnelError):
    pass


class TunnelBlockedError(TunnelError):
    pass


class TunnelClosedError(TunnelError):
    pass


class ParamError(RuntimeError):
    pass
