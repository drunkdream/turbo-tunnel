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
        elif self._protocol in ('ssh',) and port == 22:
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
        elif self.protocol in ('ssh',):
            return 22

    @property
    def address(self):
        return self.host, self.port

    @property
    def auth(self):
        return urllib.parse.unquote(self._auth)

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
