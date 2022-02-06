# -*- coding: utf-8 -*-
"""Tunnel
"""

import asyncio
import asyncio.sslproto
import inspect
import re
import socket
import ssl
import time

import tornado.iostream

from . import registry
from . import utils


class Tunnel(utils.IStream):
    """Tunnel base class"""

    timeout = 15

    def __init__(self, tunnel, url, address):
        self._tunnel = tunnel
        self._url = url
        self._addr, self._port = address or (None, None)
        if (not self._addr or not self._port) and url:
            self._addr, self._port = url.host, url.port
        self._running = True
        self._connected = False
        self._buffer = bytearray()

    def __str__(self):
        address = self._url
        if not address or not address.host:
            if address:
                address = str(address)
            else:
                address = ""
            address += "%s:%d" % (self._addr, self._port)
        return "%s [%x]%s" % (self.__class__.__name__, id(self), address)

    @classmethod
    def has_cache(cls, url):
        return False

    @property
    def socket(self):
        if self._tunnel:
            return self._tunnel.socket
        return None

    @property
    def url(self):
        return self._url

    def closed(self):
        return not self._tunnel or self._tunnel.closed()

    async def readline(self):
        while b"\n" not in self._buffer:
            self._buffer += await self.read()

        buffer, self._buffer = self._buffer.split(b"\n", 1)
        return buffer + b"\n"

    def on_read(self, buffer):
        utils.logger.debug(
            "[%s] Recv %d bytes from upstream" % (self.__class__.__name__, len(buffer))
        )

    def on_close(self):
        address = ""
        if (
            self._addr
            and self._port
            and (self._addr != self._url.host or self._port != self._url.port)
        ):
            address = "/%s/%d" % (self._addr, self._port)
        utils.logger.warn(
            "[%s] Upstream %s%s closed" % (self.__class__.__name__, self._url, address)
        )
        self.close()

    async def wait_for_connecting(self):
        time0 = time.time()
        while time.time() - time0 < self.__class__.timeout:
            if not self._connected:
                await asyncio.sleep(0.005)
            else:
                break
        else:
            raise utils.TimeoutError("Wait for connecting timeout")

    async def fork(self):
        time0 = time.time()
        tunnel = await self._tunnel.fork()
        tunnel = self.__class__(tunnel, self._url, (self._addr, self._port))
        if await tunnel.connect():
            utils.logger.info(
                "[%s][%.3f] %s fork success"
                % (self.__class__.__name__, (time.time() - time0), tunnel)
            )
            return tunnel
        return None


class TCPTunnel(Tunnel):
    """TCP Tunnel"""

    def __init__(self, tunnel, url=None, address=None, server_side=False):
        self._stream = None
        if isinstance(tunnel, socket.socket):
            self._stream = tornado.iostream.IOStream(tunnel)
        elif isinstance(tunnel, tornado.iostream.IOStream):
            self._stream = tunnel
        elif not isinstance(tunnel, Tunnel):
            raise ValueError("Invalid param: %r" % tunnel)

        self._server_side = server_side
        if self._stream and not address:
            if server_side:
                address = self._stream.socket.getsockname()
            else:
                address = self._stream.socket.getpeername()
        super(TCPTunnel, self).__init__(
            tunnel if not self._stream else None, url, address
        )

    def __getattr__(self, attr):
        if self._stream:
            try:
                return getattr(self._stream, attr)
            except AttributeError:
                raise AttributeError(
                    "'%s' object has no attribute '%s'"
                    % (self.__class__.__name__, attr)
                )
        else:
            raise AttributeError(
                "'%s' object has no attribute '%s'" % (self.__class__.__name__, attr)
            )

    @property
    def socket(self):
        if self._stream:
            return self._stream.socket
        elif self._tunnel:
            return self._tunnel.socket
        else:
            return None

    @property
    def stream(self):
        return self._stream

    def closed(self):
        if self._stream:
            return self._stream.closed()
        elif self._tunnel:
            return self._tunnel.closed()
        else:
            return True

    async def connect(self):
        if self._stream:
            addr, port = self._addr, self._port
            if not utils.is_ip_address(addr):
                addr, port = await utils.resolve_address((addr, port))
                if addr == self._addr:
                    utils.logger.warning(
                        "[%s] Resolve address %s failed"
                        % (self.__class__.__name__, addr)
                    )
                    return False
            try:
                return await self._stream.connect((addr, port))
            except tornado.iostream.StreamClosedError:
                utils.logger.warning(
                    "[%s] Connect %s:%d failed"
                    % (self.__class__.__name__, self._addr, self._port)
                )
                return False
        return True

    async def read(self):
        if self._stream:
            try:
                buffer = await self._stream.read_bytes(4096, partial=True)
            except tornado.iostream.StreamClosedError:
                pass
            else:
                if buffer:
                    return buffer
            raise utils.TunnelClosedError(self)
        elif self._tunnel:
            return await self._tunnel.read()
        else:
            raise utils.TunnelClosedError(self)

    async def read_until(self, delimiter, timeout=None):
        if self._stream:
            try:
                if not timeout:
                    buffer = await self._stream.read_until(delimiter)
                else:
                    buffer = await asyncio.wait_for(
                        self._stream.read_until(delimiter), timeout
                    )
            except tornado.iostream.StreamClosedError:
                pass
            else:
                if buffer:
                    return buffer
            raise utils.TunnelClosedError(self)
        elif self._tunnel:
            return await self._tunnel.read_until(delimiter, timeout)
        else:
            raise utils.TunnelClosedError(self)

    async def write(self, buffer):
        if self._stream:
            try:
                return await self._stream.write(buffer)
            except tornado.iostream.StreamClosedError:
                raise utils.TunnelClosedError(self)
        elif self._tunnel:
            return await self._tunnel.write(buffer)
        else:
            raise utils.TunnelClosedError(self)

    def close(self):
        message = "[%s] %s%s closed" % (
            self.__class__.__name__,
            ("Serverside " if self._server_side else ""),
            self,
        )
        if self._stream:
            self._stream.close()
            self._stream = None
            utils.logger.debug(message)
        elif self._tunnel:
            self._tunnel.close()
            self._tunnel = None
            utils.logger.debug(message)

    async def fork(self):
        if self._server_side:
            raise NotImplementedError("Serverside tunnel fork not supported")
        tunnel = None
        time0 = time.time()
        if self._stream:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
            tunnel = tornado.iostream.IOStream(s)
        else:
            tunnel = await self._tunnel.fork()
        if not tunnel:
            return None

        tunnel = self.__class__(tunnel, self._url, (self._addr, self._port))
        if await tunnel.connect():
            utils.logger.info(
                "[%s][%.3f] %s fork success"
                % (self.__class__.__name__, (time.time() - time0), tunnel)
            )
            return tunnel
        return None


class TunnelIOStream(tornado.iostream.BaseIOStream):
    """Tunnel to IOStream"""

    streams = {}

    def __new__(cls, tunnel):
        if tunnel not in cls.streams:
            instance = object.__new__(cls)
            instance.__init__(tunnel, True)
            cls.streams[tunnel] = instance
        return cls.streams[tunnel]

    def __init__(self, tunnel, real_init=False):
        if not real_init:
            return
        super(TunnelIOStream, self).__init__()
        self._tunnel = tunnel
        self._buffer = bytearray()
        self._close_callback = None
        self._read_event = asyncio.Event()
        utils.AsyncTaskManager().start_task(self.transfer_data_task())

    async def transfer_data_task(self):
        """transfer data from tunnel to iostream"""
        while self._tunnel:
            try:
                buffer = await self._tunnel.read()
            except utils.TunnelClosedError:
                break
            if buffer:
                self._buffer += buffer
                self._read_event.set()
            else:
                break

        self._read_event.set()

    async def read_bytes(self, num_bytes, partial=False):
        while True:
            if not self._buffer or (not partial and len(self._buffer) < num_bytes):
                if not self._tunnel:
                    assert not self._buffer
                    raise tornado.iostream.StreamClosedError()
                if self._read_event.is_set():
                    self._read_event.clear()
                await self._read_event.wait()

            if not self._buffer:
                raise tornado.iostream.StreamClosedError()
            elif len(self._buffer) >= num_bytes:
                buffer = self._buffer[:num_bytes]
                self._buffer = self._buffer[num_bytes:]
                if isinstance(buffer, bytearray):
                    buffer = bytes(buffer)
                return buffer
            elif partial and self._buffer:
                buffer = self._buffer
                self._buffer = bytearray()
                if isinstance(buffer, bytearray):
                    buffer = bytes(buffer)
                return buffer

    async def read_until(self, delimiter, max_bytes=None):
        while True:
            if not self._buffer:
                if not self._tunnel:
                    assert not self._buffer
                    raise tornado.iostream.StreamClosedError()
                if self._read_event.is_set():
                    self._read_event.clear()
                await self._read_event.wait()

            if not self._buffer:
                raise tornado.iostream.StreamClosedError()
            pos = self._buffer.find(delimiter)
            if pos >= 0:
                buffer = self._buffer[: pos + len(delimiter)]
                self._buffer = self._buffer[pos + len(delimiter) :]
                return buffer

    async def read_until_regex(self, regex, max_bytes=None):
        read_regex = re.compile(regex)
        while True:
            if not self._buffer:
                if not self._tunnel:
                    assert not self._buffer
                    raise tornado.iostream.StreamClosedError()
                if self._read_event.is_set():
                    self._read_event.clear()
                await self._read_event.wait()

            if not self._buffer:
                raise tornado.iostream.StreamClosedError()
            m = read_regex.search(self._buffer)
            if m is not None:
                buffer = self._buffer[: m.end()]
                self._buffer = self._buffer[m.end() :]
                return buffer

    def set_close_callback(self, callback):
        self._close_callback = callback

    def closed(self):
        return not self._tunnel or self._tunnel.closed()

    def close(self, exc_info=False):
        if self._close_callback:
            self._close_callback()
        if self._tunnel:
            self._tunnel.close()
            self._tunnel = None

    def read_from_fd(self, buf):
        if not self._buffer:
            return None
        elif len(buf) >= len(self._buffer):
            buf[:] = self._buffer
            read_size = len(self._buffer)
            self._buffer = bytearray()
            return read_size
        else:
            array_size = len(buf)
            buf[:] = self._buffer[:array_size]
            self._buffer = self._buffer[array_size:]
            return array_size

    def write_to_fd(self, data):
        utils.AsyncTaskManager().start_task(self._tunnel.write(data))
        return len(data)


class SSLTunnel(Tunnel, asyncio.Protocol):
    """SSL Tunnel"""

    class StreamReader(asyncio.Protocol):
        def __init__(self, on_connection_made):
            self._on_connection_made = on_connection_made
            self._read_event = asyncio.Event()
            self._buffer = bytearray()

        def connection_made(self, transport):
            self._on_connection_made(transport)

        async def read(self):
            await self._read_event.wait()
            self._read_event.clear()
            buffer = self._buffer
            self._buffer = bytearray()
            return buffer

        def data_received(self, buffer):
            self._buffer += buffer
            self._read_event.set()

        def connection_lost(self, exc):
            self._read_event.set()

    def __init__(
        self,
        tunnel,
        url=None,
        address=None,
        sslcontext=None,
        verify_ssl=True,
        server_hostname=None,
    ):
        super(SSLTunnel, self).__init__(tunnel, url, address)
        loop = asyncio.get_event_loop()
        self._connect_waiter = asyncio.Future()
        self._stream_reader = self.__class__.StreamReader(self.connection_made)
        if not verify_ssl or url and url.params.get("verify_ssl") == "false":
            if not sslcontext:
                sslcontext = ssl.create_default_context()
            sslcontext.check_hostname = False
            sslcontext.verify_mode = ssl.CERT_NONE
        elif url and not server_hostname:
            server_hostname = url.params.get("server_hostname")

        self._ssl_protocol = asyncio.sslproto.SSLProtocol(
            loop,
            self._stream_reader,
            sslcontext,
            self._connect_waiter,
            server_side=False,
            server_hostname=server_hostname,
        )
        self._up_transport = TunnelTransport(tunnel, self)
        self._down_transport = None

    def __str__(self):
        return "SSLTunnel(%s)" % self._tunnel

    async def connect(self):
        self._ssl_protocol.connection_made(self._up_transport)
        try:
            await self._connect_waiter
        except:
            utils.logger.exception(
                "[%s] SSL handshake failed" % self.__class__.__name__
            )
            return False
        else:
            self._connected = True
            return True

    def connection_made(self, transport):
        self._down_transport = transport

    def connection_lost(self, exc):
        utils.logger.debug("[%s] SSL connection lost" % self.__class__.__name__)
        if self._ssl_protocol:
            self._ssl_protocol.connection_lost(exc)
            self._ssl_protocol = None
        self._tunnel.close()

    async def read(self):
        if not self._connected:
            await self.wait_for_connecting()
        buffer = await self._stream_reader.read()
        if not buffer:
            raise utils.TunnelClosedError(self)
        return buffer

    async def write(self, buffer):
        if self._down_transport is None:
            raise utils.TunnelClosedError(self)
        self._down_transport.write(buffer)

    def close(self):
        if self._up_transport:
            self._up_transport.close()
            self._up_transport = None
        if self._down_transport:
            self._down_transport.close()
            self._down_transport = None

    def data_received(self, buffer):
        try:
            self._ssl_protocol.data_received(buffer)
        except ssl.CertificateError:
            utils.logger.exception(
                "[%s] SSL verify certificate failed" % self.__class__.__name__
            )


class TunnelTransport(asyncio.Transport):
    """Tunnel to Transport"""

    def __init__(self, tunnel, protocol):
        super(TunnelTransport, self).__init__()
        self._tunnel = tunnel
        self._protocol = protocol
        assert self._tunnel.socket is not None
        self._extra["socket"] = self._tunnel.socket
        self._extra["sockname"] = self._tunnel.socket.getsockname()
        self._extra["peername"] = self._tunnel.socket.getpeername()
        utils.AsyncTaskManager().start_task(self.transfer_data_task())

    def on_error(self, exc):
        self.close()

    def write(self, data):
        if self._tunnel is None:
            raise utils.TunnelClosedError(self)
        return utils.AsyncTaskManager().start_task(
            self._tunnel.write(data), self.on_error
        )

    def abort(self):
        self.close()

    def close(self):
        async def _close():
            if self._protocol:
                self._protocol.connection_lost(None)
                self._protocol = None
            if self._tunnel:
                self._tunnel.close()
                self._tunnel = None

        utils.AsyncTaskManager().start_task(_close())  # Avoid breaking ssl closing

    def _force_close(self, exc):
        self.close()
        raise exc

    def closed(self):
        return not self._tunnel or self._tunnel.closed()

    def is_closing(self):
        return self.closed()

    async def transfer_data_task(self):
        while self._tunnel:
            try:
                buffer = await self._tunnel.read()
            except utils.TunnelClosedError:
                break
            if buffer:
                self._protocol.data_received(buffer)
            else:
                break


def patch_tcp_client(tcp_client, tunn, verify_ssl=None, server_hostname=None):
    async def connect(
        host,
        port,
        af=socket.AF_UNSPEC,
        ssl_options=None,
        max_buffer_size=None,
        source_ip=None,
        source_port=None,
        timeout=None,
    ):
        if getattr(tcp_client, "_cached_streams", None) is None:
            tcp_client._cached_streams = {}
        stream = TunnelIOStream.streams.get(tunn)
        if stream is None:
            tun = tunn
            if ssl_options is not None:
                tun = SSLTunnel(
                    tun,
                    sslcontext=ssl_options,
                    verify_ssl=verify_ssl and verify_ssl != "false",
                    server_hostname=server_hostname or host,
                )
                await tun.connect()
            stream = TunnelIOStream(tun)
        return stream

    tcp_client.connect = connect


registry.tunnel_registry.register("tcp", TCPTunnel)
registry.tunnel_registry.register("ssl", SSLTunnel)
