# -*- coding: utf-8 -*-
'''Tunnel Route
'''


class TunnelRouter(object):
    '''Tunnel Router
    '''

    def __init__(self, conf):
        self._conf = conf

    async def select(self, address):
        for rule in self._conf.rules:
            if await rule.is_hit(address):
                tunnel = rule.tunnel
                tunnel = self._conf.get_tunnel(tunnel)
                if tunnel.is_blocked():
                    return 'block', None
                else:
                    return rule.id, tunnel
        else:
            # select default tunnel
            return 'default', self._conf.default_tunnel
