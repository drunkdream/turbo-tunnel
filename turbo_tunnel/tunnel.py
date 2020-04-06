# -*- coding: utf-8 -*-

'''
'''

import asyncio
import inspect
import socket
import time

import tornado.iostream

from . import registry
from . import utils


class Tunnel(object):
    '''
    '''
    timeout = 15

    def __init__(self, tunnel, url, address):
        self._tunnel = tunnel
        self._url = url
        self._addr, self._port = address or (None, None)
        if not self._addr or not self._port:
            self._addr, self._port = url.host, url.port
        self._running = True
        self._connected = False
        #asyncio.ensure_future(self.start())

    def __str__(self):
        return '%s %s' % (self.__class__.__name__, self._url)

    @property
    def socket(self):
        raise NotImplementedError('%s.%s' % (self.__class__.__name__, inspect.currentframe().f_code.co_name))

    @property
    def stream(self):
        raise NotImplementedError('%s.%s' % (self.__class__.__name__, inspect.currentframe().f_code.co_name))

    async def read(self):
        raise NotImplementedError('%s.%s' % (self.__class__.__name__, inspect.currentframe().f_code.co_name))

    def on_read(self, buffer):
        utils.logger.debug('[%s] Recv %d bytes from upstream' % (self.__class__.__name__, len(buffer)))

    async def write(self, buffer):
        raise NotImplementedError('%s.%s' % (self.__class__.__name__, inspect.currentframe().f_code.co_name))

    def close(self):
        raise NotImplementedError('%s.%s' % (self.__class__.__name__, inspect.currentframe().f_code.co_name))

    def on_close(self):
        address = ''
        if self._addr and self._port and (self._addr != self._url.host or self._port != self._url.port):
            address = '/%s/%d' % (self._addr, self._port)
        utils.logger.warn('[%s] Upstream %s%s closed' % (self.__class__.__name__, self._url, address))
        self.close()

    async def wait_for_connecting(self):
        time0 = time.time()
        while time.time() - time0 < self.__class__.timeout:
            if not self._connected:
                await tornado.gen.sleep(0.01)
            else:
                break
        else:
            raise utils.TimeoutError('Wait for connecting timeout')

    async def connect(self):
        '''
        '''
        raise NotImplementedError


class TCPTunnel(Tunnel):
    '''TCP Tunnel
    '''

    def __init__(self, tunnel, url, address):
        super(TCPTunnel, self).__init__(tunnel, url, address)

    async def connect(self):
        return True

    async def read(self):
        return await self._tunnel.read()

    async def write(self, buffer):
        return await self._tunnel.write(buffer)

    def close(self):
        pass


registry.tunnel_registry.register('tcp', TCPTunnel)
