# -*- coding: utf-8 -*-

'''Socks Tunnel
'''

import asyncio
import socket
import struct

from . import registry
from . import server
from . import tunnel
from . import utils


class Socks4RequestPacket(object):

    def __init__(self, address, userid=None):
        self._addr, self._port = address
        self._userid = userid or b''

    @property
    def address(self):
        return self._addr, self._port

    @property
    def userid(self):
        return self._userid.decode()

    def serialize(self):
        buffer = b'\x04\x01'
        buffer += struct.pack('!H', self._port)
        buffer += socket.inet_aton(self._addr)
        if self._userid:
            buffer += self._userid
        buffer += b'\x00'
        return buffer

    @staticmethod
    def unserialize_from(buffer):
        if len(buffer) < 9:
            return None, buffer
        if buffer[0] != 4:
            raise utils.TunnelPacketError('Invalid socks4 request packet: %r' % buffer)
        if buffer[1] not in (1, 2):
            raise utils.TunnelPacketError('Invalid socks4 request packet: %r' % buffer)
        elif buffer[1] == 2:
            raise NotImplementedError

        port = struct.unpack('!H', buffer[2:4])[0]
        addr = socket.inet_ntoa(buffer[4:8])
        pos = buffer.find(b'\x00', 8)
        if pos < 0:
            return None, buffer
        userid = buffer[8:pos]
        packet = Socks4RequestPacket((addr, port), userid)
        return packet, buffer[pos + 1:]


class Socks4ResponsePacket(object):

    def __init__(self, success):
        self._success = success

    @property
    def success(self):
        return self._success

    def serialize(self):
        buffer = b'\x00'
        if self._success:
            buffer += b'\x5a'
        else:
            buffer += b'\x5b'
        buffer += b'\x00\x00\x00\x00\x00\x00'
        return buffer

    @staticmethod
    def unserialize_from(buffer):
        if len(buffer) < 8:
            return None, buffer
        if buffer[0] != 0:
            raise utils.TunnelPacketError('Invalid socks4 response packet: %r' % buffer)
        if buffer[1] == 0x5a:
            success = True
        else:
            success = False
        return Socks4ResponsePacket(success), buffer[8:]


class Socks4Tunnel(tunnel.TCPTunnel):
    '''Socks4 Tunnel
    '''

    async def connect(self):
        userid = self._url.auth
        addr, port = await utils.resolve_address((self._addr, self._port))
        request = Socks4RequestPacket((addr, port), userid)
        await self._tunnel.write(request.serialize())
        buffer = await self._tunnel.read()
        response, buffer = Socks4ResponsePacket.unserialize_from(buffer)
        if not response or not response.success:
            return False
        return True

    def close(self):
        if self._tunnel:
            self._tunnel.close()
            self._tunnel = None


class Socks4TunnelServer(server.TCPTunnelServer):

    async def handle_stream(self, stream, address):
        downstream = tunnel.TCPTunnel(stream)
        buffer = await downstream.read()
        while True:
            request, buffer = Socks4RequestPacket.unserialize_from(buffer)
            if request:
                break
        assert not buffer
        target_address = request.address
        auth_data = self._listen_url.auth
        if auth_data and request.userid != auth_data:
            utils.logger.info('[%s] Connection to %s:%d refused due to wrong userid' % (self.__class__.__name__, target_address[0], target_address[1]))
            response = Socks4ResponsePacket(False)
            await downstream.write(response.serialize())
            stream.close()
            return

        with server.TunnelConnection(address, target_address) as tun_conn:
            with self.create_tunnel_chain() as tunnel_chain:
                try:
                    await tunnel_chain.create_tunnel(target_address)
                except utils.TunnelError as e:
                    utils.logger.warn(
                        '[%s] Connect %s:%d failed: %s' %
                        (self.__class__.__name__, target_address[0],
                         target_address[1], e))
                    response = Socks4ResponsePacket(False)
                    await downstream.write(response.serialize())
                    stream.close()
                    return
                response = Socks4ResponsePacket(True)
                await downstream.write(response.serialize())
                tasks = [
                    self.forward_data_to_upstream(tun_conn, downstream,
                                                  tunnel_chain.tail),
                    self.forward_data_to_downstream(tun_conn, downstream,
                                                    tunnel_chain.tail)
                ]
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                downstream.close()


registry.tunnel_registry.register('socks', Socks4Tunnel)
registry.tunnel_registry.register('socks4', Socks4Tunnel)
registry.server_registry.register('socks', Socks4TunnelServer)
registry.server_registry.register('socks4', Socks4TunnelServer)
