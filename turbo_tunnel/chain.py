# -*- coding: utf-8 -*-

'''Tunnel chain
'''

import socket

import tornado.iostream

from . import registry
from . import route
from . import tunnel
from . import utils


def _retry_func(func):
    async def func_wrapper(self, *args, **kwargs):
        for i in range(self._try_connect_count):
            try:
                return await func(self, *args, **kwargs)
            except Exception as e:
                if i < self._try_connect_count - 1:
                    utils.logger.exception('[%s] Call function %s %d failed' % (self.__class__.__name__, func.__name__, (i + 1)))
                    await tornado.gen.sleep(1)
                else:
                    raise e
    return func_wrapper


class TunnelChain(object):
    '''Tunnel Chain
    '''

    def __init__(self, tunnel_router_or_urls, try_connect_count=1):
        self._tunnel_router = self._tunnel_urls = None
        if isinstance(tunnel_router_or_urls, route.TunnelRouter):
            self._tunnel_router = tunnel_router_or_urls
        else:
            self._tunnel_urls = tunnel_router_or_urls
        self._try_connect_count = try_connect_count
        self._tunnel_list = []
        self._index = 0

    @property
    def head(self):
        if self._tunnel_list:
            return self._tunnel_list[0]
        else:
            return None

    @property
    def tail(self):
        if self._tunnel_list:
            return self._tunnel_list[-1]
        else:
            return None

    @_retry_func
    async def create_tunnel(self, address):
        if self._tunnel_router:
            selected_tunnel = self._tunnel_router.select(address)
            self._tunnel_urls = selected_tunnel.urls
            utils.logger.info('[%s] Select tunnel %s to access %s:%d' % (self.__class__.__name__, ', '.join([str(url) for url in self._tunnel_urls]), address[0], address[1]))
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        tunnel_stream = tornado.iostream.IOStream(s)
        tun = utils.TCPStream(tunnel_stream)
        self._tunnel_list.append(tun)
        tunnel_address = address
        if self._tunnel_urls and self._tunnel_urls[0].host and self._tunnel_urls[0].port:
            tunnel_address = (self._tunnel_urls[0].host, self._tunnel_urls[0].port)

        try:
            await tunnel_stream.connect(tunnel_address)
        except tornado.iostream.StreamClosedError:
            raise utils.TunnelError('Connect %s failed' % tun)

        for i, url in enumerate(self._tunnel_urls):
            tunnel_class = registry.tunnel_registry[url.protocol]
            if not tunnel_class:
                raise utils.TunnelError('%s tunnel not registered' % url.protocol.upper())
            next_address = address
            if i < len(self._tunnel_urls) - 1:
                next_url = self._tunnel_urls[i + 1]
                next_address = next_url.host, next_url.port
            tun = tunnel_class(tun, url, next_address)
            self._tunnel_list.append(tun)

            if not await tun.connect():
                raise utils.TunnelError('Connect %s failed' % tun)
            utils.logger.debug('[%s] Tunnel to %s established' % (self.__class__.__name__, url))
        utils.logger.info('[%s] Create tunnel to %s:%d success' % (self.__class__.__name__, address[0], address[1]))

    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_value, exc_trackback):
        for tunnel in self._tunnel_list:
            tunnel.close()
        self._tunnel_list = []
