# -*- coding: utf-8 -*-
"""Tunnel Chain
"""

import copy
import inspect
import socket
import time

import tornado.iostream

from . import registry
from . import route
from . import tunnel
from . import utils


class TunnelChain(object):
    """Tunnel Chain"""

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
                            "[%s] Call function %s %d failed"
                            % (self.__class__.__name__, func.__name__, (i + 1))
                        )
                        await tornado.gen.sleep(1)
                    else:
                        raise e

        return func_wrapper

    def get_cached_tunnel(self, tunnel_urls):
        for i, url in enumerate(tunnel_urls[::-1]):
            tunnel_class = registry.tunnel_registry[url.protocol]
            if not tunnel_class:
                raise utils.TunnelError(
                    "%s tunnel not registered" % url.protocol.upper()
                )
            if tunnel_class.has_cache(url):
                return len(tunnel_urls) - i - 1
        return -1

    async def select_tunnel(self, address):
        tunnel_urls = self._tunnel_urls
        if self._tunnel_router:
            selected_rule, selected_tunnel = await self._tunnel_router.select(address)
            registry.plugin_registry.notify(
                "tunnel_selected", address, selected_rule, selected_tunnel
            )
            if selected_rule == "block":
                utils.logger.warn(
                    "[%s] Address %s:%d is blocked"
                    % (self.__class__.__name__, address[0], address[1])
                )
                raise utils.TunnelBlockedError("%s:%d" % (address))

            tunnel_urls = selected_tunnel.urls
            utils.logger.info(
                "[%s] Select tunnel [%s] %s to access %s:%d"
                % (
                    self.__class__.__name__,
                    selected_rule,
                    ", ".join([str(url) for url in tunnel_urls]),
                    address[0],
                    address[1],
                )
            )
        return copy.deepcopy(tunnel_urls)

    async def get_tunnel_address(self, tunnel_url):
        tunnel_class = registry.tunnel_registry[tunnel_url.protocol]
        if not tunnel_class:
            return tunnel_url.address
        if hasattr(tunnel_class, "get_tunnel_address"):
            result = tunnel_class.get_tunnel_address(tunnel_url)
            if inspect.isawaitable(result):
                result = await result
            return result
        return tunnel_url.address

    async def create_tunnel(self, address, tunnel_urls=None):
        tunnel_urls = tunnel_urls or await self.select_tunnel(address)
        self._tunnel_urls = tunnel_urls
        if len(tunnel_urls) > 1:
            for i in range(len(tunnel_urls) - 2, -1, -1):
                tunnel_url = tunnel_urls[i]
                if tunnel_url.protocol == "tcp":
                    if tunnel_url.host:
                        utils.logger.error(
                            "[%s] Invalid tunnel chain: %s"
                            % (
                                self.__class__.__name__,
                                ", ".join([str(url) for url in tunnel_urls]),
                            )
                        )
                        raise utils.TunnelBlockedError("%s:%d" % (address))

                    tunnel_urls.pop(i)  # Ignore internal tcp:// tunnel

        tunnel_address = address
        if tunnel_urls:
            host, port = await self.get_tunnel_address(tunnel_urls[0])
            if host and port:
                tunnel_address = host, port

        cached_tunnel_index = self.get_cached_tunnel(tunnel_urls)
        if cached_tunnel_index < 0:
            if self._tunnel_router:
                tunnel_address = await self._tunnel_router.resolve(tunnel_address)
            if tunnel_urls[0].protocol == "icmp":
                tunn = None
            else:
                af = socket.AF_INET
                if utils.is_ipv6_address(tunnel_address[0]):
                    af = socket.AF_INET6
                    utils.logger.debug(
                        "[%s] Address %s is ipv6 address"
                        % (self.__class__.__name__, tunnel_address[0])
                    )
                s = socket.socket(af, socket.SOCK_STREAM, 0)

                stream = tornado.iostream.IOStream(s)
                tunn = tunnel.TCPTunnel(stream, None, tunnel_address)
                if not await tunn.connect():
                    raise utils.TunnelConnectError(
                        "Create %s to %s:%d failed" % (tunn, address[0], address[1])
                    )
            self._tunnel_list.append(tunn)
            if tunnel_urls[0].protocol == "tcp" and not tunnel_urls[0].host:
                # Avoid duplicated tcp tunnel
                tunnel_urls = tunnel_urls[1:]
        else:
            utils.logger.info(
                "[%s] Found cached tunnel %s to %s:%d"
                % (
                    self.__class__.__name__,
                    tunnel_urls[cached_tunnel_index],
                    address[0],
                    address[1],
                )
            )
            tunnel_urls = tunnel_urls[cached_tunnel_index:]
            tunn = None

        time_start = time.time()
        for i, url in enumerate(tunnel_urls):
            tunnel_class = registry.tunnel_registry[url.protocol]
            if not tunnel_class:
                raise utils.TunnelError(
                    "%s tunnel not registered" % url.protocol.upper()
                )
            next_address = address
            if i < len(tunnel_urls) - 1:
                next_url = tunnel_urls[i + 1]
                next_address = await self.get_tunnel_address(next_url)
            if self._tunnel_router:
                next_address = await self._tunnel_router.resolve(next_address)
            tunn = tunnel_class(tunn, url, next_address)
            self._tunnel_list.append(tunn)

            time0 = time.time()
            if not await tunn.connect():
                raise utils.TunnelConnectError(
                    "Create %s to %s:%d failed" % (tunn, address[0], address[1])
                )
            utils.logger.debug(
                "[%s][%.3f] Tunnel to %s established"
                % (self.__class__.__name__, (time.time() - time0), url)
            )
        utils.logger.info(
            "[%s][%.3f] Create tunnel to %s:%d success"
            % (
                self.__class__.__name__,
                (time.time() - time_start),
                address[0],
                address[1],
            )
        )

    def close(self):
        tunnel = self.tail
        if tunnel:
            tunnel.close()
            self._tunnel_list = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_trackback):
        self.close()
