# -*- coding: utf-8 -*-

'''
'''


class ServerRegistry(object):

    def __init__(self):
        self._server_list = {}
    
    def register(self, protocol, server_class):
        self._server_list[protocol] = server_class

    def __getitem__(self, index):
        return self._server_list.get(index)


class TunnelRegistry(object):

    def __init__(self):
        self._tunnel_list = {}
    
    def register(self, protocol, tunnel_class):
        self._tunnel_list[protocol] = tunnel_class

    def __getitem__(self, index):
        return self._tunnel_list.get(index)


server_registry = ServerRegistry()
tunnel_registry = TunnelRegistry()
