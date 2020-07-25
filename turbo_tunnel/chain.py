# -*- coding: utf-8 -*-
'''Tunnel Chain
'''

import socket
import time

import tornado.iostream

from . import registry
from . import route
from . import tunnel
from . import utils


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
        if self._try_connect_count > 1:
            self.create_tunnel = self._retry(self.create_tunnel)
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

    @property
    def tunnel_urls(self):
        return self._tunnel_urls

    def _retry(self, func):
        async def func_wrapper(*args, **kwargs):
            for i in range(self._try_connect_count):
                try:
                    return await func(*args, **kwargs)
                except utils.TunnelConnectError as e:
                    if i < self._try_connect_count - 1:
                        utils.logger.exception(
                            '[%s] Call function %s %d failed' %
                            (self.__class__.__name__, func.__name__, (i + 1)))
                        await tornado.gen.sleep(1)
                    else:
                        raise e

        return func_wrapper

    def get_cached_tunnel(self):
        for i, url in enumerate(self._tunnel_urls[::-1]):
            tunnel_class = registry.tunnel_registry[url.protocol]
            if not tunnel_class:
                raise utils.TunnelError('%s tunnel not registered' %
                                        url.protocol.upper())
            if tunnel_class.has_cache(url):
                return len(self._tunnel_urls) - i - 1
        return -1

    async def create_tunnel(self, address):
        if self._tunnel_router:
            selected_rule, selected_tunnel = await self._tunnel_router.select(
                address)
            registry.plugin_registry.notify('tunnel_selected', address,
                                            selected_rule, selected_tunnel)
            if selected_rule == 'block':
                utils.logger.warn(
                    '[%s] Address %s:%d is blocked' %
                    (self.__class__.__name__, address[0], address[1]))
                raise utils.TunnelBlockedError('%s:%d' % (address))

            self._tunnel_urls = selected_tunnel.urls
            utils.logger.info(
                '[%s] Select tunnel [%s] %s to access %s:%d' %
                (self.__class__.__name__, selected_rule, ', '.join(
                    [str(url)
                     for url in self._tunnel_urls]), address[0], address[1]))

        tunnel_address = address
        if self._tunnel_urls and self._tunnel_urls[
                0].host and self._tunnel_urls[0].port:
            tunnel_address = (self._tunnel_urls[0].host,
                              self._tunnel_urls[0].port)

        tunnel_urls = self._tunnel_urls[:]
        cached_tunnel_index = self.get_cached_tunnel()
        if cached_tunnel_index < 0:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
            stream = tornado.iostream.IOStream(s)
            tunn = tunnel.TCPTunnel(stream, None, tunnel_address)
            await tunn.connect()
            self._tunnel_list.append(tunn)
        else:
            utils.logger.info(
                '[%s] Found cached tunnel %s to %s:%d' %
                (self.__class__.__name__, tunnel_urls[cached_tunnel_index],
                 address[0], address[1]))
            tunnel_urls = tunnel_urls[cached_tunnel_index:]
            tunn = None

        time_start = time.time()
        for i, url in enumerate(tunnel_urls):
            tunnel_class = registry.tunnel_registry[url.protocol]
            if not tunnel_class:
                raise utils.TunnelError('%s tunnel not registered' %
                                        url.protocol.upper())
            next_address = address
            if i < len(tunnel_urls) - 1:
                next_url = tunnel_urls[i + 1]
                next_address = next_url.host, next_url.port
            tunn = tunnel_class(tunn, url, next_address)
            self._tunnel_list.append(tunn)

            time0 = time.time()
            if not await tunn.connect():
                raise utils.TunnelConnectError('Create %s to %s:%d failed' %
                                               (tunn, address[0], address[1]))
            utils.logger.debug('[%s][%.3f] Tunnel to %s established' %
                               (self.__class__.__name__,
                                (time.time() - time0), url))
        utils.logger.info('[%s][%.3f] Create tunnel to %s:%d success' %
                          (self.__class__.__name__,
                           (time.time() - time_start), address[0], address[1]))

    def close(self):
        tunnel = self.tail
        if tunnel:
            tunnel.close()
            self._tunnel_list = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_trackback):
        self.close()
