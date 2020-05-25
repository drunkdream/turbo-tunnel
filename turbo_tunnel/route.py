# -*- coding: utf-8 -*-
'''Tunnel Route
'''


class TunnelRouter(object):
    '''Tunnel Router
    '''

    def __init__(self, conf):
        self._conf = conf

    async def select(self, address):
        hit_rules = []
        for rule in self._conf.rules:
            if await rule.is_hit(address):
                hit_rules.append(rule)
        if not hit_rules:
            # select default tunnel
            return self._conf.default_tunnel
        max_priority = -1
        tunnel = None
        rule = None
        for _rule in hit_rules:
            if _rule.priority > max_priority:
                max_priority = _rule.priority
                tunnel = _rule.tunnel
                rule = _rule.id
        if tunnel:
            tunnel = self._conf.get_tunnel(tunnel)
            if tunnel.is_blocked():
                return 'block', None
            else:
                return rule, tunnel
        else:
            return 'default', self._conf.default_tunnel
