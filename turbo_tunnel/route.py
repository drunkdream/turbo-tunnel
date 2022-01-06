# -*- coding: utf-8 -*-
"""Tunnel Route
"""

from . import utils


class TunnelRouter(object):
    """Tunnel Router
    """

    def __init__(self, conf):
        self._conf = conf

    async def resolve(self, address):
        if address[0] in self._conf.hosts:
            return self._conf.hosts[address[0]], address[1]
        return address

    async def select_tunnel(self, address_list):
        for rule in self._conf.rules:
            for address in address_list:
                if await rule.is_hit(address):
                    tunnel = rule.tunnel
                    tunnel = self._conf.get_tunnel(tunnel)
                    if tunnel.is_blocked():
                        return "block", None
                    else:
                        return rule.id, tunnel
        return None, None

    async def select(self, address):
        address_list = [address]
        is_ip_address = utils.is_ip_address(address[0])
        if not is_ip_address:
            resolved_address = await self.resolve(address)
            if resolved_address != address:
                address_list.append(resolved_address)

        rule, tunnel = await self.select_tunnel(address_list)
        if rule:
            return rule, tunnel
        if not is_ip_address:
            resolved_address = await utils.resolve_address(address)
            if resolved_address != address:
                rule, tunnel = await self.select_tunnel(address_list)
                if rule:
                    return rule, tunnel
        # select default tunnel
        return "default", self._conf.default_tunnel
