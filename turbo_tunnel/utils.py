# -*- coding: utf-8 -*-
'''Miscellaneous utility functions and classes
'''

import inspect
import logging
import socket
import urllib.parse

import tornado.iostream
import tornado.netutil

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
    def auth(self):
        return self._auth

    @property
    def path(self):
        return self._path

    @path.setter
    def path(self, value):
        self._path = value


class IStream(object):
    @property
    def socket(self):
        raise NotImplementedError(
            '%s.%s' %
            (self.__class__.__name__, inspect.currentframe().f_code.co_name))

    @property
    def stream(self):
        raise NotImplementedError(
            '%s.%s' %
            (self.__class__.__name__, inspect.currentframe().f_code.co_name))

    @property
    def target_address(self):
        raise NotImplementedError(
            '%s.%s' %
            (self.__class__.__name__, inspect.currentframe().f_code.co_name))

    async def connect(self):
        raise NotImplementedError(
            '%s.%s' %
            (self.__class__.__name__, inspect.currentframe().f_code.co_name))

    async def read(self):
        raise NotImplementedError(
            '%s.%s' %
            (self.__class__.__name__, inspect.currentframe().f_code.co_name))

    async def write(self, buffer):
        raise NotImplementedError(
            '%s.%s' %
            (self.__class__.__name__, inspect.currentframe().f_code.co_name))

    def close(self):
        raise NotImplementedError(
            '%s.%s' %
            (self.__class__.__name__, inspect.currentframe().f_code.co_name))


class TCPStream(IStream):
    '''IOStream Wrapper
    '''

    def __init__(self, socket_or_stream):
        if isinstance(socket_or_stream, socket.socket):
            self._stream = tornado.iostream.IOStream(socket_or_stream)
        else:
            self._stream = socket_or_stream

    def __getattr__(self, attr):
        try:
            return getattr(self._stream, attr)
        except AttributeError:
            raise AttributeError("'%s' object has no attribute '%s'" %
                                 (self.__class__.__name__, attr))

    @property
    def socket(self):
        return self._stream.socket

    @property
    def stream(self):
        return self._stream

    @property
    def target_address(self):
        if self.socket:
            return self.socket.getpeername()
        else:
            return None

    async def connect(self, address):
        return await self._stream.connect(address)

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
        if not self._stream:
            raise TunnelClosedError
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


class TunnelPacketError(TunnelError):
    pass


class ParamError(RuntimeError):
    pass


async def resolve_address(address):
    if not tornado.netutil.is_valid_ip(address[0]):
        resovler = tornado.netutil.Resolver()
        addr_list = await resovler.resolve(*address)
        return addr_list[0][1]
    else:
        return address
