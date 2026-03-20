# -*- coding: utf-8 -*-
"""Tunnel Chain"""

import asyncio
import copy
import inspect
import socket
import time
from typing import Any, Awaitable, Callable, List, Optional, Tuple, Type, TypeVar, Union

import tornado.iostream

from . import conf
from . import registry
from . import route
from . import tunnel
from . import utils

_T = TypeVar("_T")


class TunnelChain(object):
    """Tunnel Chain"""

    _tunnel_router: Optional[route.TunnelRouter]
    _tunnel_urls: Optional[List[utils.Url]]
    _try_connect_count: int
    _tunnel_list: List[Optional[tunnel.Tunnel]]
    _index: int

    def __init__(
        self,
        tunnel_router_or_urls: Union[route.TunnelRouter, List[utils.Url]],
        try_connect_count: int = 1,
    ) -> None:
        self._tunnel_router = None
        self._tunnel_urls = None
        if isinstance(tunnel_router_or_urls, route.TunnelRouter):
            self._tunnel_router = tunnel_router_or_urls
        else:
            self._tunnel_urls = tunnel_router_or_urls
        self._try_connect_count = try_connect_count
        if self._try_connect_count > 1:
            self.create_tunnel = self._retry(self.create_tunnel)  # type: ignore[assignment,method-assign]
        self._tunnel_list = []
        self._index = 0

    @property
    def head(self) -> Optional[tunnel.Tunnel]:
        """Get the first tunnel in the chain."""
        if self._tunnel_list:
            return self._tunnel_list[0]
        else:
            return None

    @property
    def tail(self) -> Optional[tunnel.Tunnel]:
        """Get the last tunnel in the chain."""
        if self._tunnel_list:
            return self._tunnel_list[-1]
        else:
            return None

    @property
    def tunnel_urls(self) -> Optional[List[utils.Url]]:
        """Get the list of tunnel URLs."""
        return self._tunnel_urls

    def _retry(
        self, func: Callable[..., Awaitable[None]]
    ) -> Callable[..., Awaitable[None]]:
        """Wrap a function to retry on TunnelConnectError."""

        async def func_wrapper(*args: Any, **kwargs: Any) -> None:
            for i in range(self._try_connect_count):
                try:
                    await func(*args, **kwargs)
                    return
                except utils.TunnelConnectError as e:
                    if i < self._try_connect_count - 1:
                        utils.logger.exception(
                            "[%s] Call function %s %d failed"
                            % (self.__class__.__name__, func.__name__, (i + 1))
                        )
                        await asyncio.sleep(1)
                    else:
                        raise e

        return func_wrapper

    def get_cached_tunnel(self, tunnel_urls: List[utils.Url]) -> int:
        """Find the index of the first cached tunnel in the URL list.

        Args:
            tunnel_urls: List of tunnel URLs to check

        Returns:
            The index of the cached tunnel, or -1 if no cached tunnel found
        """
        for i, url in enumerate(tunnel_urls[::-1]):
            tunnel_class = registry.tunnel_registry[url.protocol]
            if not tunnel_class:
                raise utils.TunnelError(
                    "%s tunnel not registered" % url.protocol.upper()
                )
            if tunnel_class.has_cache(url):
                return len(tunnel_urls) - i - 1
        return -1

    async def select_tunnel(self, address: Tuple[str, int]) -> List[utils.Url]:
        """Select tunnel URLs for the given address.

        Args:
            address: The (host, port) tuple to route

        Returns:
            List of tunnel URLs to use for the connection

        Raises:
            TunnelBlockedError: If the address is blocked
        """
        tunnel_urls: Optional[List[utils.Url]] = self._tunnel_urls
        if self._tunnel_router:
            selected_rule, selected_tunnel = await self._tunnel_router.select(address)
            registry.plugin_registry.notify(
                "tunnel_selected", address, selected_rule, selected_tunnel
            )
            if selected_rule == "block":
                utils.logger.warning(
                    "[%s] Address %s:%d is blocked"
                    % (self.__class__.__name__, address[0], address[1])
                )
                raise utils.TunnelBlockedError("%s:%d" % (address))

            if selected_tunnel:
                tunnel_urls = selected_tunnel.urls
            utils.logger.info(
                "[%s] Select tunnel [%s] %s to access %s:%d"
                % (
                    self.__class__.__name__,
                    selected_rule,
                    ", ".join([str(url) for url in (tunnel_urls or [])]),
                    address[0],
                    address[1],
                )
            )
        return copy.deepcopy(tunnel_urls or [])

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

    async def create_tunnel(
        self, address: Tuple[str, int], tunnel_urls: Optional[List[utils.Url]] = None
    ) -> None:
        """Create a tunnel chain to the target address.

        Args:
            address: The target (host, port) tuple to connect to
            tunnel_urls: Optional list of tunnel URLs to use; if None, will select automatically

        Raises:
            TunnelBlockedError: If the address is blocked
            TunnelConnectError: If connection to tunnel fails
            TunnelError: If tunnel protocol is not registered
        """
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

                    _ = tunnel_urls.pop(i)  # Ignore internal tcp:// tunnel

        tunnel_address: Tuple[str, int] = address
        if tunnel_urls:
            host, port = await self.get_tunnel_address(tunnel_urls[0])
            if host and port:
                tunnel_address = host, port

        cached_tunnel_index: int = self.get_cached_tunnel(tunnel_urls)
        tunn: Optional[tunnel.Tunnel] = None

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

        time_start: float = time.time()
        for i, url in enumerate(tunnel_urls):
            tunnel_class = registry.tunnel_registry[url.protocol]
            if not tunnel_class:
                raise utils.TunnelError(
                    "%s tunnel not registered" % url.protocol.upper()
                )
            next_address: Tuple[str, int] = address
            if i < len(tunnel_urls) - 1:
                next_url = tunnel_urls[i + 1]
                next_address = await self.get_tunnel_address(next_url)
            if (
                self._tunnel_router
                and url.params.get("server_resolve", "false") != "true"
            ):
                next_address = await self._tunnel_router.resolve(next_address)

            tunn = tunnel_class(tunn, url, next_address)
            self._tunnel_list.append(tunn)

            time0: float = time.time()
            if tunn and not await tunn.connect():
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

    def close(self) -> None:
        """Close the tunnel chain."""
        tun = self.tail
        if tun:
            tun.close()
            self._tunnel_list = []

    def __enter__(self) -> "TunnelChain":
        """Context manager entry."""
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        exc_trackback: Any,
    ) -> None:
        """Context manager exit."""
        self.close()
