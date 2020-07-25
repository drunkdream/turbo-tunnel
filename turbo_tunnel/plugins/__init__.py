# -*- coding: utf-8 -*-
'''Internal plugins
'''


class Plugin(object):
    def on_load(self):
        pass

    def on_unload(self):
        pass

    def on_tunnel_selected(self, address, rule, tunnel):
        pass

    def on_new_connection(self, connection):
        pass

    def on_tunnel_address_updated(self, connection, tunnel_address):
        pass

    def on_data_recevied(self, connection, buffer):
        pass

    def on_data_sent(self, connection, buffer):
        pass

    def on_connection_closed(self, connection):
        pass
