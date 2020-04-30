# -*- coding: utf-8 -*-

'''Tunnel Route
'''

class TunnelRouter(object):
    '''Tunnel Router
    '''

    def __init__(self, conf):
        self._conf = conf

    def select(self, address):
        hit_rules = []
        for rule in self._conf.rules:
            if rule.is_hit(address):
                hit_rules.append(rule)
        if not hit_rules:
            # select default tunnel
            return self._conf.default_tunnel
        max_priority = -1
        tunnel = None
        for rule in hit_rules:
            if rule.priority > max_priority:
                max_priority = rule.priority
                tunnel = rule.tunnel
        if tunnel:
            tunnel = self._conf.get_tunnel(tunnel)
            if tunnel.is_blocked():
                return None
            else:
                return tunnel
        else:
            return self._conf.default_tunnel
