# -*- coding: utf-8 -*-
'''SSH Tunnel
'''

import asyncio

import asyncssh

from . import registry
from . import tunnel
from . import utils


class SSHTunnel(tunnel.Tunnel):
    '''SSH Tunnel
    '''
    ssh_conns = {}

    def __init__(self, tunnel, url, address):
        super(SSHTunnel, self).__init__(tunnel, url, address)
        self._reader = None
        self._writer = None
        self._closed = False

    @classmethod
    def has_cache(cls, url):
        key = '%s:%d' % url.address
        if key in cls.ssh_conns:
            conn = cls.ssh_conns[key]
            if conn._transport.closed():
                utils.logger.warn('[%s] SSH connection %s closed, remove cache' % (cls.__name__, key))
                cls.ssh_conns.pop(key)
                return False
            return True
        return False

    async def create_ssh_conn(self):
        key = '%s:%d' % (self._url.address)
        if key not in self.__class__.ssh_conns:
            loop = asyncio.get_event_loop()
            options = {
                'known_hosts': None
            }
            if self._url.auth:
                username, password = self._url.auth.split(':', 1)
                options['username'] = username
                options['password'] = password
            options = asyncssh.SSHClientConnectionOptions(**options)
            utils.logger.info(
                '[%s] Create connection to ssh server %s:%d' %
                (self.__class__.__name__, self._url.host, self._url.port))
            ssh_conn = asyncssh.SSHClientConnection(self._url.host,
                                                    self._url.port,
                                                    loop,
                                                    options=options,
                                                    wait='auth')
            transport = tunnel.TunnelTransport(self._tunnel, ssh_conn)
            ssh_conn.connection_made(transport)
            try:
                await ssh_conn.wait_established()
            except asyncssh.misc.PermissionDenied as e:
                utils.logger.error('[%s] Connect ssh server %s:%d auth failed: %s' % (self.__class__.__name__, self._url.host, self._url.port, e))
                ssh_conn.abort()
                await ssh_conn.wait_closed()
                return None
            except Exception:
                ssh_conn.abort()
                await ssh_conn.wait_closed()
                return None
            self.__class__.ssh_conns[key] = ssh_conn
        return self.__class__.ssh_conns[key]

    async def connect(self):
        ssh_conn = await self.create_ssh_conn()
        if not ssh_conn:
            return False
        this = self

        class SSHTCPSession(asyncssh.SSHTCPSession):
            def data_received(self, data, datatype):
                this._buffer += data

            def connection_lost(self, exc):
                this._closed = True

        self._reader, self._writer = await ssh_conn.open_connection(
            self._addr, self._port)
        return True

    async def read(self):
        if self._reader:
            buffer = await self._reader.read(4096)
            if buffer:
                return buffer
        raise utils.TunnelClosedError()

    async def write(self, buffer):
        if self._writer:
            return self._writer.write(buffer)
        else:
            raise utils.TunnelClosedError()

    def close(self):
        if self._writer:
            self._writer.write_eof()
            self._writer = None
        self._reader = None


registry.tunnel_registry.register('ssh', SSHTunnel)
