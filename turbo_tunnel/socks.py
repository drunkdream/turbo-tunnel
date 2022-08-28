# -*- coding: utf-8 -*-

"""Socks Tunnel
"""

import asyncio
import random
import socket
import struct

from . import registry
from . import server
from . import tunnel
from . import utils


class EnumSocks5AuthMethod(object):

    NO_AUTH = 0
    GSSAPI = 1
    PASSWORD = 2
    NOT_SUPPORTED = 255


class EnumSocks5Command(object):

    CONNECT = 1
    BIND = 2
    UDP_ASSOCIATE = 3


class EnumSocks5AddressType(object):

    IPV4 = 1
    DOMAIN = 3
    IPV6 = 4


class Socks4RequestPacket(object):
    def __init__(self, address, userid=None):
        self._addr, self._port = address
        self._userid = userid or b""

    @property
    def address(self):
        return self._addr, self._port

    @property
    def userid(self):
        return self._userid.decode()

    def serialize(self):
        buffer = b"\x04\x01"
        buffer += struct.pack("!H", self._port)
        buffer += socket.inet_aton(self._addr)
        if self._userid:
            buffer += self._userid
        buffer += b"\x00"
        return buffer

    @staticmethod
    def unserialize_from(buffer):
        if len(buffer) < 9:
            return None, buffer
        if buffer[0] != 4:
            raise utils.TunnelPacketError("Invalid socks4 request packet: %r" % buffer)
        if buffer[1] not in (1, 2):
            raise utils.TunnelPacketError("Invalid socks4 request packet: %r" % buffer)
        elif buffer[1] == 2:
            raise NotImplementedError

        port = struct.unpack("!H", buffer[2:4])[0]
        addr = socket.inet_ntoa(buffer[4:8])
        pos = buffer.find(b"\x00", 8)
        if pos < 0:
            return None, buffer
        userid = buffer[8:pos]
        packet = Socks4RequestPacket((addr, port), userid)
        return packet, buffer[pos + 1 :]


class Socks4ResponsePacket(object):
    def __init__(self, success):
        self._success = success

    @property
    def success(self):
        return self._success

    def serialize(self):
        buffer = b"\x00"
        if self._success:
            buffer += b"\x5a"
        else:
            buffer += b"\x5b"
        buffer += b"\x00\x00\x00\x00\x00\x00"
        return buffer

    @staticmethod
    def unserialize_from(buffer):
        if len(buffer) < 8:
            return None, buffer
        if buffer[0] != 0:
            raise utils.TunnelPacketError("Invalid socks4 response packet: %r" % buffer)
        if buffer[1] == 0x5A:
            success = True
        else:
            success = False
        return Socks4ResponsePacket(success), buffer[8:]


class Socks4Tunnel(tunnel.TCPTunnel):
    """Socks4 Tunnel"""

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
            utils.logger.info(
                "[%s] Connection to %s:%d refused due to wrong userid"
                % (self.__class__.__name__, target_address[0], target_address[1])
            )
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
                        "[%s] Connect %s:%d failed: %s"
                        % (
                            self.__class__.__name__,
                            target_address[0],
                            target_address[1],
                            e,
                        )
                    )
                    response = Socks4ResponsePacket(False)
                    await downstream.write(response.serialize())
                    stream.close()
                    return
                response = Socks4ResponsePacket(True)
                await downstream.write(response.serialize())
                tasks = [
                    self.forward_data_to_upstream(
                        tun_conn, downstream, tunnel_chain.tail
                    ),
                    self.forward_data_to_downstream(
                        tun_conn, downstream, tunnel_chain.tail
                    ),
                ]
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                downstream.close()


class Socks5AuthRequestPacket(object):
    """Auth Request Packet"""

    def __init__(self, auth_methods=None):
        self._auth_methods = auth_methods or [
            EnumSocks5AuthMethod.NO_AUTH,
            EnumSocks5AuthMethod.PASSWORD,
            EnumSocks5AuthMethod.GSSAPI,
        ]

    @property
    def auth_methods(self):
        return self._auth_methods

    def serialize(self):
        buffer = b"\x05"
        buffer += struct.pack("B", len(self._auth_methods))
        for method in self._auth_methods:
            buffer += struct.pack("B", method)
        return buffer

    @staticmethod
    def unserialize_from(buffer):
        if len(buffer) < 3 or buffer[1] != len(buffer) - 2:
            raise utils.TunnelPacketError("Invalid auth request packet")
        if buffer[0] != 5:
            raise utils.TunnelPacketError(
                "Invalid socks protocol version: %x" % buffer[0]
            )
        auth_methods = []
        for method in buffer[2:]:
            auth_methods.append(method)
        return Socks5AuthRequestPacket(auth_methods)


class Socks5AuthResponsePacket(object):
    """Auth Response Packet"""

    def __init__(self, auth_method):
        self._auth_method = auth_method

    @property
    def auth_method(self):
        return self._auth_method

    def serialize(self):
        buffer = b"\x05"
        buffer += struct.pack("B", self._auth_method)
        return buffer

    @staticmethod
    def unserialize_from(buffer):
        if len(buffer) != 2:
            raise utils.TunnelPacketError("Invalid auth response packet")
        if buffer[0] != 5:
            raise utils.TunnelPacketError(
                "Invalid socks protocol version: %x" % buffer[0]
            )
        return Socks5AuthResponsePacket(buffer[1])


class Socks5PasswordAuthRequestPacket(object):
    """Password Auth Request Packet"""

    def __init__(self, username, password):
        self._username = username
        self._password = password

    @property
    def username(self):
        return self._username

    @property
    def password(self):
        return self._password

    def serialize(self):
        buffer = b"\x01"
        username, password = self._username.encode(), self._password.encode()
        buffer += struct.pack("B", len(username))
        buffer += username
        buffer += struct.pack("B", len(password))
        buffer += password
        return buffer

    @staticmethod
    def unserialize_from(buffer):
        if len(buffer) < 5:
            raise utils.TunnelPacketError("Invalid password auth request packet")
        offset = 1
        username_len = buffer[offset]
        offset += 1
        if len(buffer) < offset + username_len:
            raise utils.TunnelPacketError("Invalid password auth request packet")
        username = buffer[offset : offset + username_len].decode()
        offset += username_len
        password_len = buffer[offset]
        offset += 1
        if len(buffer) < offset + password_len:
            raise utils.TunnelPacketError("Invalid password auth request packet")
        password = buffer[offset : offset + password_len].decode()
        return Socks5PasswordAuthRequestPacket(username, password)


class Socks5PasswordAuthResponsePacket(object):
    """Password Auth Response Packet"""

    def __init__(self, result):
        self._result = result

    @property
    def result(self):
        return self._result

    def serialize(self):
        buffer = b"\x01"
        buffer += struct.pack("B", self._result)
        return buffer

    @staticmethod
    def unserialize_from(buffer):
        if len(buffer) != 2:
            raise utils.TunnelPacketError("Invalid password auth response packet")
        return Socks5PasswordAuthResponsePacket(buffer[1])


class Socks5ConnectRequestPacket(object):
    """Socks5 Connect Request Packet"""

    def __init__(self, command, address):
        self._command = command
        self._address = address

    @property
    def command(self):
        return self._command

    @property
    def address(self):
        return self._address

    def serialize(self):
        buffer = b"\x05"
        buffer += struct.pack("B", self._command)
        buffer += b"\x00"
        if not utils.is_ip_address(self._address[0]):
            domain = self._address[0].encode()
            buffer += struct.pack("BB", EnumSocks5AddressType.DOMAIN, len(domain))
            buffer += domain
            buffer += struct.pack("!H", self._address[1])
        else:
            buffer += struct.pack("B", EnumSocks5AddressType.IPV4)
            buffer += socket.inet_aton(self._address[0])
            buffer += struct.pack("!H", self._address[1])
        return buffer

    @staticmethod
    def unserialize_from(buffer):
        if len(buffer) < 6:
            raise utils.TunnelPacketError("Invalid connect request packet")
        if buffer[0] != 5:
            raise utils.TunnelPacketError(
                "Invalid socks protocol version: %x" % buffer[0]
            )
        command = buffer[1]
        address_type = buffer[3]
        address = None
        if address_type == EnumSocks5AddressType.IPV4:
            if len(buffer) != 10:
                raise utils.TunnelPacketError("Invalid ipv4 connect request packet")
            address = (
                socket.inet_ntoa(buffer[4:8]),
                struct.unpack("!H", buffer[8:10])[0],
            )
        elif address_type == EnumSocks5AddressType.IPV6:
            if len(buffer) != 22:
                raise utils.TunnelPacketError("Invalid ipv6 connect request packet")
            address = (
                socket.inet_ntop(socket.AF_INET6, buffer[4:20]),
                struct.unpack("!H", buffer[20:22])[0],
            )
        elif address_type == EnumSocks5AddressType.DOMAIN:
            domain_len = buffer[4]
            address = (
                buffer[5 : 5 + domain_len].decode(),
                struct.unpack("!H", buffer[5 + domain_len : 7 + domain_len])[0],
            )
        else:
            raise NotImplementedError("Unsupported address type: %d" % address_type)
        return Socks5ConnectRequestPacket(command, address)


class Socks5ConnectResponsePacket(object):
    """Socks5 Connect Response Packet"""

    def __init__(self, result, address):
        self._result = result
        self._address = address

    @property
    def result(self):
        return self._result

    def serialize(self):
        buffer = b"\x05"
        buffer += struct.pack("B", self._result)
        buffer += b"\x00"
        if not utils.is_ip_address(self._address[0]):
            domain = self._address[0].encode()
            buffer += struct.pack("BB", EnumSocks5AddressType.DOMAIN, len(domain))
            buffer += domain
            buffer += struct.pack("!H", self._address[1])
        else:
            buffer += struct.pack("B", EnumSocks5AddressType.IPV4)
            buffer += socket.inet_aton(self._address[0])
            buffer += struct.pack("!H", self._address[1])
        return buffer

    @staticmethod
    def unserialize_from(buffer):
        if len(buffer) < 7:
            raise utils.TunnelPacketError("Invalid connect response packet")
        if buffer[0] != 5:
            raise utils.TunnelPacketError(
                "Invalid socks protocol version: %x" % buffer[0]
            )
        result = buffer[1]
        address_type = buffer[3]
        address = None
        if address_type == EnumSocks5AddressType.IPV4:
            if len(buffer) != 10:
                raise utils.TunnelPacketError("Invalid ipv4 connect response packet")
            address = (
                socket.inet_ntoa(buffer[4:8]),
                struct.unpack("!H", buffer[8:10])[0],
            )
        else:
            raise NotImplementedError("Unsupported address type: %d" % address_type)
        return Socks5ConnectResponsePacket(result, address)


class Socks5Tunnel(tunnel.TCPTunnel):
    """Socks5 Tunnel"""

    async def connect(self):
        auth_data = self._url.auth
        username, password = None, None
        if ":" in auth_data:
            username, password = auth_data.split(":", 1)
        auth_request = Socks5AuthRequestPacket()
        await self._tunnel.write(auth_request.serialize())
        buffer = await self._tunnel.read()
        auth_response = Socks5AuthResponsePacket.unserialize_from(buffer)
        if auth_response.auth_method == EnumSocks5AuthMethod.PASSWORD:
            if not username or not password:
                utils.logger.warn(
                    "[%s] Password is required for auth" % self.__class__.__name__
                )
                return False
            password_request = Socks5PasswordAuthRequestPacket(username, password)
            await self._tunnel.write(password_request.serialize())
            buffer = await self._tunnel.read()
            password_response = Socks5PasswordAuthResponsePacket.unserialize_from(
                buffer
            )
            if password_response.result:
                utils.logger.warn(
                    "[%s] Socks5 auth with password failed: %d"
                    % (self.__class__.__name__, password_response.result)
                )
                return False
        elif auth_response.auth_method == EnumSocks5AuthMethod.NOT_SUPPORTED:
            utils.loggger.warn(
                "[%s] Socks5 server not support auth methods: %s"
                % (self.__class__.__name__, auth_request.auth_methods)
            )
            return False
        elif auth_response.auth_method != EnumSocks5AuthMethod.NO_AUTH:
            raise NotImplementedError(
                "Unsupported auth method: %d" % auth_response.auth_method
            )
        connect_request = Socks5ConnectRequestPacket(
            EnumSocks5Command.CONNECT, (self._addr, self._port)
        )
        await self._tunnel.write(connect_request.serialize())
        buffer = await self._tunnel.read()
        connect_response = Socks5ConnectResponsePacket.unserialize_from(buffer)
        if connect_response.result:
            utils.logger.warn(
                "[%s] Connect %s:%d failed: %d"
                % (
                    self.__class__.__name__,
                    self._addr,
                    self._port,
                    connect_response.result,
                )
            )
            return False
        return True


class Socks5UDPForwardPacket(object):
    """Socks5 UDP Forward Packet"""

    def __init__(self, address, payload):
        self._address = address
        self._payload = payload
        self._frag = 0

    @property
    def address(self):
        return self._address

    @property
    def payload(self):
        return self._payload

    def serialize(self):
        buffer = b"\x00\x00"
        buffer += struct.pack("B", self._frag)
        if not utils.is_ip_address(self._address[0]):
            domain = self._address[0].encode()
            buffer += struct.pack("BB", EnumSocks5AddressType.DOMAIN, len(domain))
            buffer += domain
            buffer += struct.pack("!H", self._address[1])
        else:
            buffer += struct.pack("B", EnumSocks5AddressType.IPV4)
            buffer += socket.inet_aton(self._address[0])
            buffer += struct.pack("!H", self._address[1])
        buffer += self._payload
        return buffer

    @staticmethod
    def unserialize_from(buffer):
        if len(buffer) < 7:
            raise utils.TunnelPacketError("Invalid connect response packet")
        frag = buffer[2]
        if frag:
            raise NotImplementedError("Socks5 UDP frag not supported")
        address = None
        address_type = buffer[3]
        offset = 4
        if address_type == EnumSocks5AddressType.IPV4:
            if len(buffer) <= 10:
                raise utils.TunnelPacketError("Invalid ipv4 connect request packet")
            address = (
                socket.inet_ntoa(buffer[4:8]),
                struct.unpack("!H", buffer[8:10])[0],
            )
            offset = 10
        elif address_type == EnumSocks5AddressType.DOMAIN:
            domain_len = buffer[4]
            address = (
                buffer[5 : 5 + domain_len].decode(),
                struct.unpack("!H", buffer[5 + domain_len : 7 + domain_len])[0],
            )
            offset = 7 + domain_len
        else:
            raise NotImplementedError("Unsupported address type: %d" % address_type)
        payload = buffer[offset:]
        return Socks5UDPForwardPacket(address, payload)


class UDPProxyProtocol(object):
    def __init__(self, handler):
        self._handler = handler

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        utils.safe_ensure_future(
            self._handler.on_data_received(addr, self.transport, data)
        )

    def error_received(self, exc):
        utils.logger.warning("[%s] Error received: %s" % (self.__class__.__name__, exc))

    def connection_lost(self, exc):
        utils.logger.info("[%s] UDP connection closed" % self.__class__.__name__)


class UDPProxyServer(object):
    def __init__(self, listen_addr):
        self._listen_addr = listen_addr
        self._server_transport = None
        self._transports = {}
        self._running = True

    async def create_server(self):
        self._server_transport = await self.create_transport(
            local_addr=self._listen_addr
        )
        return self._server_transport

    async def serve_forever(self):
        while self._running:
            await asyncio.sleep(0.1)

    def close(self):
        self._server_transport.close()
        self._server_transport = None
        for key in self._transports:
            _, source_transport, target_transport = self._transports[key]
            source_transport.close()
            target_transport.close()
        self._transports = []
        self._running = False

    async def create_transport(self, local_addr=None, remote_addr=None):
        if not local_addr and not remote_addr:
            raise ValueError("local_addr or remote_addr MUST be specified")
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: UDPProxyProtocol(self),
            local_addr=local_addr,
            remote_addr=remote_addr,
        )
        return transport

    async def on_data_received(self, addr, source_transport, data):
        transport = None
        buffer = b""
        address = None
        if addr in self._transports:
            # recv from server
            address, source_transport, target_transport = self._transports[addr]
            utils.logger.debug(
                "[%s] UDP %s => %s" % (self.__class__.__name__, address, addr)
            )
            forward_packet = Socks5UDPForwardPacket(addr, data)
            buffer = forward_packet.serialize()
            transport = source_transport
        else:
            forward_packet = Socks5UDPForwardPacket.unserialize_from(data)
            buffer = forward_packet.payload
            utils.logger.debug(
                "[%s] UDP %s => %s"
                % (self.__class__.__name__, addr, forward_packet.address)
            )
            target_transport = await self.create_transport(
                remote_addr=forward_packet.address
            )
            if forward_packet.address not in self._transports:
                self._transports[forward_packet.address] = (
                    addr,
                    source_transport,
                    target_transport,
                )
            transport = target_transport

        transport.sendto(buffer, address)


class Socks5TunnelServer(server.TCPTunnelServer):
    """Socks5 Tunnel Server"""

    def post_init(self):
        super(Socks5TunnelServer, self).post_init()
        self._enable_udp = self._listen_url.params.get("enable_udp", "0") == "1"

    async def handle_stream(self, stream, address):
        auth = self._listen_url.auth
        downstream = tunnel.TCPTunnel(stream)
        buffer = await downstream.read()
        auth_request = Socks5AuthRequestPacket.unserialize_from(buffer)

        if ":" in auth and EnumSocks5AuthMethod.PASSWORD in auth_request.auth_methods:
            username, password = auth.split(":", 1)
            auth_response = Socks5AuthResponsePacket(EnumSocks5AuthMethod.PASSWORD)
            await downstream.write(auth_response.serialize())
            buffer = await downstream.read()
            password_request = Socks5PasswordAuthRequestPacket.unserialize_from(buffer)
            if (
                password_request.username != username
                or password_request.password != password
            ):
                utils.logger.warn(
                    "[%s] Password verify failed: %s:%s"
                    % (
                        self.__class__.__name__,
                        password_request.username,
                        password_request.password,
                    )
                )
                password_response = Socks5PasswordAuthResponsePacket(1)
                await downstream.write(password_response.serialize())
                stream.close()
                return
            else:
                password_response = Socks5PasswordAuthResponsePacket(0)
                await downstream.write(password_response.serialize())
        elif auth:
            auth_response = Socks5AuthResponsePacket(EnumSocks5AuthMethod.NOT_SUPPORTED)
            stream.close()
        else:
            auth_response = Socks5AuthResponsePacket(EnumSocks5AuthMethod.NO_AUTH)
            await downstream.write(auth_response.serialize())

        buffer = await downstream.read()
        connect_request = Socks5ConnectRequestPacket.unserialize_from(buffer)
        target_address = connect_request.address
        resolved_target_address = await utils.resolve_address(target_address)
        if resolved_target_address[0] == target_address[0]:
            resolved_target_address = ("255.255.255.255", resolved_target_address[1])

        if connect_request.command == EnumSocks5Command.UDP_ASSOCIATE:
            listen_address = None
            udp_proxy = None
            if not self._enable_udp:
                utils.logger.info(
                    "[%s] UDP proxy is disabled" % self.__class__.__name__
                )
                connect_response = Socks5ConnectResponsePacket(2, ("127.0.0.1", 0))
                await downstream.write(connect_response.serialize())
                downstream.close()
                return
            while True:
                listen_address = ("127.0.0.1", random.randint(10000, 65535))
                udp_proxy = UDPProxyServer(listen_address)
                try:
                    await udp_proxy.create_server()
                except OSError as ex:
                    if ex.errno == 98:
                        # Address already in use
                        utils.logger.info(
                            "[%s] UDP port %d is already listening"
                            % (self.__class__.__name__, listen_address[1])
                        )
                        continue
                    else:
                        raise ex
                else:
                    break
            connect_response = Socks5ConnectResponsePacket(0, listen_address)
            await downstream.write(connect_response.serialize())
            await stream.read_until_close()
            udp_proxy.close()
            return
        elif connect_request.command != EnumSocks5Command.CONNECT:
            utils.logger.warn(
                "[%s] Only CONNECT command is supported: %s"
                % (self.__class__.__name__, connect_request.command)
            )
            connect_response = Socks5ConnectResponsePacket(7, connect_request.address)
            await downstream.write(connect_response.serialize())
            stream.close()
            return

        with server.TunnelConnection(address, target_address) as tun_conn:
            with self.create_tunnel_chain() as tunnel_chain:
                try:
                    await tunnel_chain.create_tunnel(target_address)
                except utils.TunnelError as e:
                    utils.logger.warn(
                        "[%s] Connect %s:%d failed: %s"
                        % (
                            self.__class__.__name__,
                            target_address[0],
                            target_address[1],
                            e,
                        )
                    )
                    connect_response = Socks5ConnectResponsePacket(
                        5, resolved_target_address
                    )
                    await downstream.write(connect_response.serialize())
                    stream.close()
                    return

                connect_response = Socks5ConnectResponsePacket(
                    0, resolved_target_address
                )
                await downstream.write(connect_response.serialize())
                tasks = [
                    self.forward_data_to_upstream(
                        tun_conn, downstream, tunnel_chain.tail
                    ),
                    self.forward_data_to_downstream(
                        tun_conn, downstream, tunnel_chain.tail
                    ),
                ]
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                downstream.close()


registry.tunnel_registry.register("socks", Socks4Tunnel)
registry.tunnel_registry.register("socks4", Socks4Tunnel)
registry.server_registry.register("socks", Socks4TunnelServer)
registry.server_registry.register("socks4", Socks4TunnelServer)
registry.tunnel_registry.register("socks5", Socks5Tunnel)
registry.server_registry.register("socks5", Socks5TunnelServer)
