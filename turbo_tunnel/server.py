# -*- coding: utf-8 -*-
'''Tunnel Server
'''

import asyncio
import socket

import tornado.tcpserver

from . import chain
from . import registry
from . import route
from . import tunnel
from . import utils


class TunnelServer(object):
    '''Tunnel Server
    '''
    retry_count = 0

    def __new__(cls, listen_url, tunnel_router_or_urls):
        listen_url = utils.Url(listen_url)
        tunnel_router = None
        tunnel_urls = []
        if isinstance(tunnel_router_or_urls, route.TunnelRouter):
            tunnel_router = tunnel_router_or_urls
        else:
            tunnel_urls = [utils.Url(url) for url in tunnel_router_or_urls]
        server_class = registry.server_registry[listen_url.protocol]
        if not server_class:
            raise RuntimeError('%s tunnel server not registered' %
                               listen_url.protocol.upper())
        for tunnel in tunnel_urls:
            if not registry.tunnel_registry[tunnel.protocol]:
                raise RuntimeError('%s tunnel not registered' %
                                   tunnel.protocol.upper())
        instance = object.__new__(server_class)
        instance.__init__(listen_url, tunnel_router, tunnel_urls, True)
        return instance

    def __init__(self,
                 listen_url,
                 tunnel_router=None,
                 tunnel_urls=None,
                 real_init=False):
        if not real_init:
            return
        self._listen_url = listen_url
        self._tunnel_router = tunnel_router
        self._tunnel_urls = tunnel_urls
        self._running = True
        self.post_init()

    @property
    def final_tunnel(self):
        for tunnel_url in self._tunnel_urls[::-1]:
            if not tunnel_url.host or not tunnel_url.port:
                continue
            return tunnel_url
        return None

    def post_init(self):
        pass

    def close(self):
        self._running = False

    def create_tunnel_chain(self):
        return chain.TunnelChain(self._tunnel_router or self._tunnel_urls,
                                 self.retry_count + 1)

    async def forward_data_to_upstream(self, tun_conn, downstream, upstream):
        while self._running:
            try:
                buffer = await downstream.read()
            except utils.TunnelClosedError:
                tun_conn.on_downstream_closed()
                upstream.close()
                break

            try:
                await upstream.write(buffer)
            except utils.TunnelClosedError:
                tun_conn.on_upstream_closed()
                downstream.close()
                break
            else:
                tun_conn.on_data_sent(buffer)

    async def forward_data_to_downstream(self, tun_conn, downstream, upstream):
        while self._running:
            try:
                buffer = await upstream.read()
            except utils.TunnelClosedError:
                tun_conn.on_upstream_closed()
                downstream.close()
                break
            else:
                tun_conn.on_data_recevied(buffer)

            try:
                await downstream.write(buffer)
            except utils.TunnelClosedError:
                tun_conn.on_downstream_closed()
                upstream.close()
                break

    def start(self):
        raise NotImplementedError


class TunnelConnection(object):
    '''Tunnel Connection
    '''
    def __init__(self, client_address, target_address, tunnel_address=None):
        self._client_address = client_address
        self._target_address = target_address
        self._tunnel_address = tunnel_address
        self._bytes_sent = 0
        self._bytes_received = 0

    def __enter__(self):
        self.on_open()
        return self

    def __exit__(self, exc_type, exc_value, exc_trackback):
        self.on_close()

    @property
    def client_address(self):
        return self._client_address

    @property
    def target_address(self):
        return self._target_address

    @property
    def tunnel_address(self):
        return self._tunnel_address

    def update_tunnel_address(self, tunnel_address):
        self._tunnel_address = tunnel_address
        registry.plugin_registry.notify('tunnel_address_updated', self,
                                        tunnel_address)

    def on_open(self):
        message = '[%s] New connection from %s:%d' % (self.__class__.__name__,
                                                      self._client_address[0],
                                                      self._client_address[1])
        message += ', tunnel to %s:%d' % self._target_address
        if self._tunnel_address:
            message += ' through %s:%d' % self._tunnel_address
        utils.logger.info(message)
        registry.plugin_registry.notify('new_connection', self)

    def on_data_recevied(self, buffer):
        self._bytes_received += len(buffer)
        utils.logger.debug('[%s][%s:%d][%s:%d] %d bytes recevied' %
                           (self.__class__.__name__, self._client_address[0],
                            self._client_address[1], self._target_address[0],
                            self._target_address[1], len(buffer)))
        registry.plugin_registry.notify('data_recevied', self, buffer)

    def on_data_sent(self, buffer):
        self._bytes_sent += len(buffer)
        utils.logger.debug('[%s][%s:%d][%s:%d] %d bytes sent' %
                           (self.__class__.__name__, self._client_address[0],
                            self._client_address[1], self._target_address[0],
                            self._target_address[1], len(buffer)))
        registry.plugin_registry.notify('data_sent', self, buffer)

    def on_upstream_closed(self):
        utils.logger.info('[%s][%s:%d][%s:%d] Upstream closed' %
                          (self.__class__.__name__, self._client_address[0],
                           self._client_address[1], self._target_address[0],
                           self._target_address[1]))

    def on_downstream_closed(self):
        utils.logger.info('[%s][%s:%d][%s:%d] Downstream closed' %
                          (self.__class__.__name__, self._client_address[0],
                           self._client_address[1], self._target_address[0],
                           self._target_address[1]))

    def on_close(self):
        utils.logger.debug(
            '[%s][%s:%d][%s:%d] Connection closed, total %d bytes sent, %d bytes received'
            %
            (self.__class__.__name__, self._client_address[0],
             self._client_address[1], self._target_address[0],
             self._target_address[1], self._bytes_sent, self._bytes_received))
        registry.plugin_registry.notify('connection_closed', self)


class TCPTunnelServer(TunnelServer, tornado.tcpserver.TCPServer):
    '''TCP Tunnel Server
    '''
    def post_init(self):
        tornado.tcpserver.TCPServer.__init__(self)

    @property
    def final_tunnel(self):
        for tunnel_url in self._tunnel_urls[:-1][::-1]:
            if not tunnel_url.host or not tunnel_url.port:
                continue
            return tunnel_url
        return None

    async def handle_stream(self, stream, address):
        target_address = self._tunnel_urls[-1].host, self._tunnel_urls[-1].port
        downstream = tunnel.TCPTunnel(stream)
        with TunnelConnection(address, target_address, self.final_tunnel
                              and self.final_tunnel.address) as tun_conn:
            with self.create_tunnel_chain() as tunnel_chain:
                try:
                    await tunnel_chain.create_tunnel(target_address)
                except utils.TunnelError as e:
                    utils.logger.warn(
                        '[%s] Connect %s:%d failed: %s' %
                        (self.__class__.__name__, target_address[0],
                         target_address[1], e))
                    stream.close()
                    return

                tasks = [
                    utils.AsyncTaskManager().wrap_task(
                        self.forward_data_to_upstream(tun_conn, downstream,
                                                      tunnel_chain.tail)),
                    utils.AsyncTaskManager().wrap_task(
                        self.forward_data_to_downstream(
                            tun_conn, downstream, tunnel_chain.tail))
                ]
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                downstream.close()

    def start(self):
        self.listen(self._listen_url.port, self._listen_url.host)
        utils.logger.info('[%s] TCP server is listening on %s:%d' %
                          (self.__class__.__name__, self._listen_url.host,
                           self._listen_url.port))


registry.server_registry.register('tcp', TCPTunnelServer)
