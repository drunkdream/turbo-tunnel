# -*- coding: utf-8 -*-
"""Tunnel Route"""

import sys
from typing import List, Optional, Tuple, Union

if sys.version_info >= (3, 8):
    from typing import Literal
else:
    from typing_extensions import Literal

from . import conf
from . import utils


class TunnelRouter(object):
    """Tunnel Router"""

    _conf: "conf.TunnelConfiguration"

    def __init__(self, conf: "conf.TunnelConfiguration") -> None:
        self._conf = conf

    async def resolve(self, address: Tuple[str, int]) -> Tuple[str, int]:
        """Resolve address using hosts mapping or DNS resolution.

        Args:
            address: The (host, port) tuple to resolve

        Returns:
            The resolved (host, port) tuple
        """
        if address[0] in self._conf.hosts:
            return self._conf.hosts[address[0]], address[1]
        return await utils.resolve_address(address)

    async def select_tunnel(
        self, address_list: List[Tuple[str, int]]
    ) -> Union[
        Tuple[Literal["block"], None], Tuple[str, "conf.Tunnel"], Tuple[None, None]
    ]:
        """Select tunnel based on rules matching the address list.

        Args:
            address_list: List of (host, port) tuples to match against rules

        Returns:
            A tuple of (rule_id, tunnel) or (None, None) if no rule matched,
            or ("block", None) if the tunnel is blocked
        """
        for rule in self._conf.rules:
            for address in address_list:
                if await rule.is_hit(address):
                    tunnel_id: str = rule.tunnel
                    tunnel: "conf.Tunnel" = self._conf.get_tunnel(tunnel_id)
                    if tunnel.is_blocked():
                        return "block", None
                    else:
                        return rule.id, tunnel
        return None, None

    async def select(
        self, address: Tuple[str, int]
    ) -> Tuple[str, Optional["conf.Tunnel"]]:
        """Select tunnel for the given address.

        Args:
            address: The (host, port) tuple to route

        Returns:
            A tuple of (rule_id, tunnel) where rule_id can be:
            - "block" with None tunnel if blocked
            - A rule id string with the matched tunnel
            - "default" with the default tunnel if no rule matched
        """
        address_list: List[Tuple[str, int]] = [address]
        is_ip_address: bool = utils.is_ip_address(address[0])
        if not is_ip_address:
            resolved_address = await self.resolve(address)
            if resolved_address != address:
                address_list.append(resolved_address)

        rule, tunnel = await self.select_tunnel(address_list)
        if rule:
            return rule, tunnel
        # select default tunnel
        return "default", self._conf.default_tunnel
