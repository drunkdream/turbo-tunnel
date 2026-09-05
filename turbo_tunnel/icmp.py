# -*- coding: utf-8 -*-

"""ICMP Tunnel
"""

import asyncio
import hashlib
import hmac
import os
import random
import socket
import struct
import sys
import time

import msgpack

from . import registry
from . import server
from . import tunnel
from . import utils


# Default delay (in seconds) before the server replies to a client PING with a
# PONG. Aligned with PING_INTERVAL (= 5s) so a PONG is always answered within one
# keepalive window. A 5s delay can be dropped en-route on some NAT/firewall
# networks; override per-server via the `ping_timeout` URL parameter (formerly
# `timeout` / `pong_delay`), e.g. `icmp://0.0.0.0?ping_timeout=0.5`.
PING_TIMEOUT_DEFAULT = 5


class EnumICMPType(object):
    ECHO_REPLY = 0
    DESTINATION_UNREACHABLE = 3
    SOURCE_QUENCH = 4
    REDIRECT = 5
    ECHO = 8
    ROUTER_ADVERTISEMENT = 9
    ROUTER_SELECTION = 10
    TIME_EXCEEDED = 11
    PARAMETER_PROBLEM = 12
    TIMESTAMP = 13
    TIMESTAMP_REPLY = 14
    INFORMATION_REQUEST = 15
    INFORMATION_REPLY = 16
    ADDRESS_MASK_REQUEST = 17
    ADDRESS_MASK_REPLY = 18
    TRACEROUTE = 30


class EnumICMPEchoCode(object):
    NO_CODE = 0


class ICMPPacket(object):
    """ICMP Packet"""

    def __init__(self, type, code, ident, seq, data):
        self._type = type
        self._code = code
        self._ident = ident
        self._seq = seq
        self._data = data

    @property
    def type(self):
        return self._type

    @property
    def code(self):
        return self._code

    @property
    def ident(self):
        return self._ident

    @property
    def sequence(self):
        return self._seq

    @property
    def data(self):
        return self._data

    def serialize(self):
        buffer = struct.pack(
            "!BBHHH", self._type, self._code, 0, self._ident, self._seq
        )
        buffer += self._data
        checksum = utils.checksum(buffer)
        return buffer[:2] + struct.pack("!H", checksum) + buffer[4:]

    @staticmethod
    def unserialize_from(buffer):
        if len(buffer) < 8:
            raise utils.TunnelPacketError("Invalid ICMP packet: %r" % buffer)
        type, code, checksum, ident, seq = struct.unpack("!BBHHH", buffer[:8])
        data = buffer[8:]
        if utils.checksum(buffer) != 0:
            raise utils.TunnelPacketError(
                "Invalid ICMP packet checksum: %.4x" % (checksum)
            )
        return ICMPPacket(type, code, ident, seq, data)


class ICMPProtocol(object):
    def __init__(self):
        self.transport = None
        self._buffers = {}
        self._event = asyncio.Event()

    @property
    def buffers(self):
        return self._buffers

    async def wait_for_receiving(self):
        await self._event.wait()
        self._event.clear()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, address):
        if sys.platform == "win32":
            # In SIO_RCVALL mode the socket receives the full IP datagram for
            # every protocol; only keep ICMP (protocol number 1) and drop the
            # rest so they don't pollute the ICMP receive buffers.
            if len(data) < 20 or data[9] != 1:
                return
        addr, _ = address
        if addr not in self._buffers:
            self._buffers[addr] = []
        self._buffers[addr].append(data[20:])  # Ignore 20 bytes ip header
        self._event.set()

    def error_received(self, exc):
        print(exc)


def _get_default_local_ip():
    """Best-effort lookup of a non-loopback IPv4 address for SIO_RCVALL binding."""
    import socket as _sock

    s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
    try:
        # UDP connect does not send traffic; it just makes the OS pick an egress
        # interface, so getsockname() returns the corresponding local IP.
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


@utils.Singleton
class AsyncICMPSocket(object):
    """Async ICMP Socket"""

    def __init__(self, loop=None):
        self._loop = loop or asyncio.get_event_loop()
        try:
            self._sock = socket.socket(
                socket.AddressFamily.AF_INET,
                socket.SOCK_RAW,
                socket.getprotobyname("icmp"),
            )
            if sys.platform == "win32":
                # On Windows a plain raw ICMP socket only receives the replies to
                # packets *we* sent; it never sees the echo requests destined to
                # this host, so a server could never accept a connection. Enabling
                # SIO_RCVALL puts the socket into promiscuous mode so it receives
                # every ICMP datagram (with the 20-byte IP header, which
                # ICMPProtocol strips off). Requires administrator privilege.
                local_ip = _get_default_local_ip()
                self._sock.bind((local_ip, 0))
                SIO_RCVALL = 0x98000001
                RCVALL_ON = 1
                self._sock.ioctl(SIO_RCVALL, RCVALL_ON)
        except PermissionError:
            if sys.platform == "win32":
                msg = "Raw socket needs administrator privilege on Windows, please run as Administrator"
            else:
                msg = (
                    "Raw socket needs root privilege (CAP_NET_RAW) on %s. "
                    "Run as root (e.g. `sudo turbo-tunnel ...`), or grant the "
                    "capability once with: "
                    "sudo setcap cap_net_raw+ep $(readlink -f $(command -v python3))"
                    % sys.platform
                )
            raise RuntimeError(msg)
        else:
            self._sock.setblocking(False)
        self._transport = None
        self._protocol = ICMPProtocol()

    async def start(self):
        if not self._transport:
            self._transport = await self.create_transport()

    async def create_transport(self, remote_addr=None):
        waiter = self._loop.create_future()
        transport = self._loop._make_datagram_transport(
            self._sock, self._protocol, None, waiter
        )
        try:
            await waiter
        except:
            transport.close()
            raise
        return transport

    def sendto(
        self,
        buffer,
        address,
        type=EnumICMPType.ECHO,
        code=EnumICMPEchoCode.NO_CODE,
        ident=None,
        seq=None,
    ):
        assert self._transport is not None
        address = (address, 0)
        if ident is None:
            ident = random.randint(0, 65535)
        if seq is None:
            seq = random.randint(1, 65535)
        icmp_packet = ICMPPacket(type, code, ident, seq, buffer)
        self._transport.sendto(icmp_packet.serialize(), address)
        utils.logger.verbose(
            "[%s] Send %d bytes icmp data to %s, ident=%d, seq=%d"
            % (
                self.__class__.__name__,
                len(buffer),
                address[0],
                ident,
                seq,
            )
        )
        return seq

    async def recvfrom(self, address=None, type=None, code=None):
        assert self._transport is not None
        while True:
            buffer = b""
            src_addr = None
            if (
                address
                and address in self._protocol.buffers
                and self._protocol.buffers[address]
            ):
                buffer = self._protocol.buffers[address].pop(0)
                src_addr = address
            elif not address:
                for addr in self._protocol.buffers:
                    if self._protocol.buffers[addr]:
                        buffer = self._protocol.buffers[addr].pop(0)
                        src_addr = addr
                        break
            if not buffer:
                await self._protocol.wait_for_receiving()
                continue

            try:
                icmp_packet = ICMPPacket.unserialize_from(buffer)
            except utils.TunnelPacketError as ex:
                utils.logger.warning(
                    "[%s][%s][%d] Decode icmp packet failed: %s"
                    % (self.__class__.__name__, src_addr, len(buffer), ex)
                )
                continue

            if (type and icmp_packet.type != type) or (
                code and icmp_packet.code != code
            ):
                utils.logger.info(
                    "[%s] Ignore unexpected icmp packet: type=%d code=%d ident=%d sequence=%d length=%d"
                    % (
                        self.__class__.__name__,
                        icmp_packet.type,
                        icmp_packet.code,
                        icmp_packet.ident,
                        icmp_packet.sequence,
                        len(icmp_packet.data),
                    )
                )
                continue
            return src_addr, icmp_packet


class ICMPTransportPacket(object):
    """ICMP Transport Packet

    |           Magic Flag          |
    | ------------- 4 ------------- |
    |  Total length |   Checksum    |
    | ----- 2 ----- | ----- 2 ----- |
    |      Seq      |      Ack      |
    | ----- 2 ----- | ----- 2 ----- |
    |  Opcode (C2S) |    Reply      |
    | ----- 1 ----- | ----- 1 ----- |
    |  Local Port  |  Remote Port  |
    | ----- 2 ----- | ----- 2 ----- |
    |            Padding            |
    """

    MAGIC_FLAG = b"TTUN"

    # Operation code -- first event byte. Identifies the action this packet
    # requests or carries (a client->server request, or a server->client probe).
    # Operation code -- the single byte that identifies what this packet does.
    # PONG / OK / FAIL / CHAL are independent opcodes (not replies layered on top
    # of another opcode), so the protocol needs no separate "reply" byte. The
    # high bit of this byte carries the direction (see _DIRECTION_MASK).
    OP_NONE = 0
    OP_PING = 1           # liveness probe
    OP_PONG = 2           # reply to OP_PING
    OP_CONNECT = 3        # establish a session
    OP_CLOSE = 4          # graceful close
    OP_RESET = 5          # hard reset
    OP_AUTH = 6           # authentication response (challenge answer)
    OP_CHAL = 7           # challenge, sent in reply to OP_CONNECT when auth is required
    OP_DATA = 8           # application payload
    OP_OK = 9             # success result (CONNECT or AUTH accepted)
    OP_FAIL = 10          # failure result (CONNECT or AUTH rejected)

    # Maximum payload carried by a single packet. Sized to stay within the
    # PPPoE MTU budget: 1492 - 20 (IP) - 8 (ICMP) - 18 (this header) = 1446,
    # so 1440 leaves a small margin and avoids IP fragmentation.
    MAX_DATA_SIZE = 1440
    # Lower bound for the configurable payload size. IPv4 only guarantees an
    # MTU of 576, minus 48 bytes of headers, so 512 still leaves headroom.
    MIN_DATA_SIZE = 512

    # Header size in bytes: magic(4) + length(2) + checksum(2) + seq(2) + ack(2)
    # + opcode(1) + client_port(2) + server_port(2) = 17.
    HEADER_LENGTH = 17

    # Direction marker, encoded in the high bit of the opcode byte.
    # C->S packets use DIRECTION_CLIENT_TO_SERVER, S->C packets use
    # DIRECTION_SERVER_TO_CLIENT. A kernel echo copies the whole payload, so the
    # marker is echoed too -- the receiver can then tell a peer packet apart from
    # its own kernel-generated echo reply. The opcode itself stays within 0..127.
    DIRECTION_CLIENT_TO_SERVER = 0
    DIRECTION_SERVER_TO_CLIENT = 1
    _DIRECTION_MASK = 0x80

    _OP_NAMES = {
        OP_NONE: "NONE",
        OP_PING: "PING",
        OP_PONG: "PONG",
        OP_CONNECT: "CONNECT",
        OP_CLOSE: "CLOSE",
        OP_RESET: "RESET",
        OP_AUTH: "AUTH",
        OP_CHAL: "CHAL",
        OP_DATA: "DATA",
        OP_OK: "OK",
        OP_FAIL: "FAIL",
    }

    @staticmethod
    def format_op(opcode):
        return ICMPTransportPacket._OP_NAMES.get(opcode, str(opcode))

    def __init__(self, seq, ack, opcode, client_port, server_port, padding, direction=0):
        self._seq = seq
        self._ack = ack
        self._opcode = opcode
        self._client_port = client_port
        self._server_port = server_port
        self._padding = padding
        self._direction = direction

    @property
    def seq_num(self):
        return self._seq

    @property
    def ack_num(self):
        return self._ack

    @property
    def event(self):
        return self._opcode

    @property
    def opcode(self):
        return self._opcode

    @property
    def client_port(self):
        return self._client_port

    @property
    def server_port(self):
        return self._server_port

    @property
    def padding(self):
        return self._padding

    @property
    def direction(self):
        return self._direction

    def serialize(self):
        buffer = self.__class__.MAGIC_FLAG
        # Encode the direction marker into the high bit of the opcode byte. It is
        # stripped back on parse, so the opcode value stays within 0..127.
        direction_bit = self.__class__._DIRECTION_MASK if self._direction else 0
        opcode_byte = (self._opcode & 0x7F) | direction_bit
        buffer += struct.pack(
            "!HHHH",
            self.__class__.HEADER_LENGTH + len(self._padding),
            0,
            self._seq,
            self._ack,
        )
        buffer += bytes([opcode_byte])
        buffer += struct.pack("!HH", self._client_port, self._server_port)
        buffer += self._padding
        checksum = utils.checksum(buffer)
        buffer = buffer[:6] + struct.pack("!H", checksum) + buffer[8:]
        # No padding is appended: the receiver restores the real payload length
        # from the total_length field, so padding would only waste bandwidth.
        return buffer

    @staticmethod
    def unserialize_from(buffer):
        header_length = ICMPTransportPacket.HEADER_LENGTH
        if len(buffer) < header_length or not buffer.startswith(ICMPTransportPacket.MAGIC_FLAG):
            raise utils.TunnelPacketError("Invalid ICMP transport packet: %r" % buffer)
        total_length, checksum, seq, ack = struct.unpack("!HHHH", buffer[4:12])
        if total_length < header_length or total_length > len(buffer):
            # Header itself is header_length bytes, so a smaller length is bogus.
            raise utils.TunnelPacketError(
                "Invalid ICMP transport packet length: %d, buffer length is %d"
                % (total_length, len(buffer))
            )
        if total_length < len(buffer):
            padding_buffer = buffer[total_length - len(buffer) :]
            if padding_buffer == b"\x00" * len(padding_buffer):
                buffer = buffer[:total_length]
        if total_length != len(buffer):
            raise utils.TunnelPacketError(
                "Invalid ICMP transport packet length: %d vs %d"
                % (total_length, len(buffer))
            )
        if utils.checksum(buffer) != 0:
            raise utils.TunnelPacketError(
                "Invalid ICMP transport packet checksum: %0.4x" % (checksum)
            )
        opcode_byte = buffer[12]
        direction = 1 if (opcode_byte & ICMPTransportPacket._DIRECTION_MASK) else 0
        opcode = opcode_byte & ~ICMPTransportPacket._DIRECTION_MASK
        client_port, server_port = struct.unpack("!HH", buffer[13:17])
        return ICMPTransportPacket(
            seq, ack, opcode, client_port, server_port, buffer[17:], direction=direction
        )


def _pack_msgpack_payload(obj):
    """Serialize a control message as a msgpack-encoded dict. Used for every
    control OP (CONNECT/PING/PONG/AUTH/CHAL/OK/FAIL/...). Each ICMP transport
    frame carries exactly one control OP, so its padding is the complete payload
    -- no length prefix / framing is needed (unlike OP_DATA's StreamForwardPacket,
    which is framed inside a reassembled byte stream)."""
    return msgpack.dumps(obj)


def _unpack_msgpack_payload(buffer):
    """Inverse of `_pack_msgpack_payload`. Raises TunnelPacketError on malformed
    input (non-msgpack bytes or truncated data). The control OP padding is
    already sliced to its exact length by ICMPTransportPacket.unserialize_from,
    so no length-prefix / strict trailing-data handling is needed."""
    try:
        return msgpack.loads(buffer, raw=False)
    except (msgpack.exceptions.ExtraData, msgpack.exceptions.FormatError,
            msgpack.exceptions.OutOfData) as ex:
        raise utils.TunnelPacketError("Invalid control payload: %s" % ex)


class ICMPSessionStream(object):
    """ICMP Transport Session Stream"""

    DELAY_ACK_TIME = 0.1

    def __init__(
        self,
        sock,
        client_address,
        server_address,
        server_side=True,
        ping_timeout=PING_TIMEOUT_DEFAULT,
        max_data_size=None,
    ):
        self._sock = sock
        self._client_address = client_address
        self._server_address = server_address
        self._server_side = server_side
        self._ping_timeout = ping_timeout
        # Per-session max packet payload, negotiated by the client via the CONNECT
        # payload (mss). None means "not negotiated"; the server then caps payloads
        # at its own configured default (self._sock.max_data_size).
        self._max_data_size = None
        if max_data_size:
            self._max_data_size = max(
                ICMPTransportPacket.MIN_DATA_SIZE,
                min(max_data_size, ICMPTransportPacket.MAX_DATA_SIZE),
            )
        # Most recent (ident, seq) seen on a client->server packet. Used by the
        # server to forge PONG replies without consuming a response slot (see
        # ICMPTransportServerSocket.send). PONG is a keepalive control packet and
        # must not compete with downlink data for the slot pool, or it starves.
        self._last_client_icmp = None
        self._status = ICMPTransportSocket.STATUS_CONNECTING
        self._seq = 0
        self._last_seq = 0
        self._last_send_ack = 0
        self._recv_seqs = []
        self._response_slots = []
        self._recving_buffers = {}
        self._recv_buffer = b""
        self._recv_event = asyncio.Event()
        self._last_alive_time = 0
        self._last_data_time = 0
        self._last_pong_time = 0
        # Last time we sent ANY reply (downlink data, PONG, ...) to the client.
        # Used to avoid sending a redundant keepalive PONG when we've already
        # replied with data within the pong window.
        self._last_reply_time = 0
        self._auth_nonce = None
        self._auth_time = 0

    @property
    def max_data_size(self):
        return self._max_data_size if self._max_data_size is not None else self._sock.max_data_size

    @property
    def ping_timeout(self):
        return self._ping_timeout

    @property
    def last_client_icmp(self):
        return self._last_client_icmp

    @property
    def auth_nonce(self):
        return self._auth_nonce

    @auth_nonce.setter
    def auth_nonce(self, value):
        self._auth_nonce = value
        self._auth_time = time.time()

    @property
    def auth_time(self):
        return self._auth_time

    def __eq__(self, other):
        if not isinstance(other, ICMPSessionStream):
            return False
        return (
            self._client_address == other.client_address
            and self._server_address == other.server_address
        )

    @property
    def client_address(self):
        return self._client_address

    @property
    def server_address(self):
        return self._server_address

    @property
    def status(self):
        return self._status

    @status.setter
    def status(self, new_status):
        self._status = new_status

    @property
    def next_seq(self):
        self._seq += 1
        return self._seq

    @property
    def next_ack(self):
        if len(self._recv_seqs) > 1:
            min_seq = min(self._recv_seqs)
            while (min_seq + 1) in self._recv_seqs:
                self._recv_seqs.remove(min_seq)
                min_seq += 1
            assert self._recv_seqs
        return self._recv_seqs[0] if self._recv_seqs else 0

    @property
    def last_alive_time(self):
        return self._last_alive_time

    @property
    def last_data_time(self):
        return self._last_data_time

    @property
    def last_reply_time(self):
        return self._last_reply_time

    @last_reply_time.setter
    def last_reply_time(self, value):
        self._last_reply_time = value

    def reset(self):
        self._status = ICMPTransportSocket.STATUS_DISCONNECT
        self._recv_event.set()

    async def read(self):
        if not self._recv_buffer:
            await self._recv_event.wait()
            self._recv_event.clear()
        if self._status == ICMPTransportSocket.STATUS_DISCONNECT:
            raise utils.TunnelClosedError()
        buffer = self._recv_buffer
        self._recv_buffer = b""
        return buffer

    async def write(self, buffer):
        if self._server_side:
            return await self._sock.send_message(self, buffer)
        else:
            return await self._sock.send_message(buffer)

    async def close(self):
        await self._sock.send(self, ICMPTransportPacket.OP_CLOSE)
        self._status = ICMPTransportSocket.STATUS_DISCONNECT

    def closed(self):
        return self._status == ICMPTransportSocket.STATUS_DISCONNECT

    def on_send_ack(self, ack_num):
        if ack_num <= 0:
            return
        if ack_num > self._last_send_ack:
            self._last_send_ack = ack_num

    def on_message_received(self, trans_packet):
        self._last_alive_time = time.time()
        seq_num = trans_packet.seq_num
        ack_num = trans_packet.ack_num
        if seq_num > 0 and seq_num not in self._recv_seqs:
            self._recv_seqs.append(seq_num)
            if len(self._recv_seqs) > 1:
                self._recv_seqs.sort()
        self._sock.on_ack_received(
            self._client_address if self._server_side else self._server_address, ack_num
        )
        if seq_num == 0:
            # ignore the message
            return
        if seq_num in self._recving_buffers or seq_num <= self._last_seq:
            utils.logger.info(
                "[%s] Ignore duplicated message %d" % (self.__class__.__name__, seq_num)
            )
        else:
            delay_time = (
                self.__class__.DELAY_ACK_TIME
                if len(trans_packet.padding) < self._sock.max_data_size
                else 0
            )
            utils.safe_ensure_future(
                self.delay_ack(
                    seq_num,
                    delay_time,
                )
            )

    def on_data_received(self, seq_num, buffer):
        self._last_data_time = time.time()
        self._recving_buffers[seq_num] = buffer
        while (self._last_seq + 1) in self._recving_buffers:
            buffer = self._recving_buffers.pop(self._last_seq + 1)
            if buffer:
                self._recv_buffer += buffer
                self._recv_event.set()
            self._last_seq += 1

    @property
    def slot_count(self):
        return len(self._response_slots)

    def clean_expired_slots(self):
        """Drop slots that are too old to be used, return removed count."""
        expire_time = ICMPTransportSocket.PING_INTERVAL + 1
        now = time.time()
        slots = []
        for it in self._response_slots:
            if now - it[2] < expire_time:
                slots.append(it)
        removed = len(self._response_slots) - len(slots)
        if removed:
            self._response_slots = slots
            utils.logger.debug(
                "[%s] Cleaned %d expired response slots, %d remained"
                % (self.__class__.__name__, removed, len(slots))
            )
        return removed

    def clear_response_slots(self):
        count = len(self._response_slots)
        if count:
            self._response_slots = []
        return count

    def add_response_slot(self, ident, seq):
        self._response_slots.append((ident, seq, time.time()))
        # Remember the newest client (ident, seq) so the server can reply with a
        # PONG without burning a response slot (keepalive packets must not
        # contend with downlink data for slots, or they get starved/dropped).
        self._last_client_icmp = (ident, seq)

    def has_response_slot(self):
        return len(self._response_slots) > 0

    async def get_response_slot(self):
        start_time = time.time()
        while not self._response_slots:
            await asyncio.sleep(0.005)
            if time.time() - start_time >= 10:
                utils.logger.warning(
                    "[%s] Waiting for response slot too slow" % self.__class__.__name__
                )
        waiting_time = time.time() - start_time
        if waiting_time >= 0.2:
            utils.logger.warning(
                "[%s][%s:%d][%s:%d] Waiting for response slot cost %.2fs"
                % (
                    self.__class__.__name__,
                    self._client_address[0],
                    self._client_address[1],
                    self._server_address[0],
                    self._server_address[1],
                    waiting_time,
                )
            )
        result = self._response_slots.pop(0)
        if time.time() - result[2] >= ICMPTransportSocket.PING_INTERVAL + 1:
            utils.logger.info(
                "[%s] Response slot %d,%d timeout: %.2fs"
                % (
                    self.__class__.__name__,
                    result[0],
                    result[1],
                    time.time() - result[2],
                )
            )
            return await self.get_response_slot()
        return result[0], result[1]

    async def delay_ack(
        self,
        seq_num,
        delay_time,
        icmp_seq=None,
    ):
        last_send_ack = self._last_send_ack
        await asyncio.sleep(delay_time)
        if (
            seq_num == 0
            and self._last_send_ack > last_send_ack
            or seq_num > 0
            and seq_num <= self._last_send_ack
        ):
            # ack already send to peer
            if seq_num > 0:
                utils.logger.verbose(
                    "[%s] Ignore send ack of message %d"
                    % (self.__class__.__name__, seq_num)
                )
            return
        event = (
            ICMPTransportPacket.OP_PONG
            if self._server_side
            else ICMPTransportPacket.OP_PING
        )

        if self._server_side:
            # PONG is a keepalive control packet. It does NOT consume a response
            # slot (downlink data already drains the pool, and contending for it
            # starved the PONGs and caused the client's ping to time out). We
            # reply to *every* PING with a PONG carrying that PING's own icmp_seq,
            # so the client can correlate and clear each pending ping. The only
            # guard is that we know the client's icmp ident to address the reply.
            if icmp_seq is None or self.last_client_icmp is None:
                utils.logger.verbose(
                    "[%s] Ignore send pong packet, no client icmp ident yet"
                    % self.__class__.__name__
                )
                return
            # If we have already sent the client some other reply (downlink data,
            # etc.) within the pong window, that packet itself proves the channel
            # is alive, so there is no need to spend a PONG just for keepalive.
            if time.time() - self.last_reply_time < delay_time:
                utils.logger.verbose(
                    "[%s] Skip pong, recent reply sent %.2fs ago"
                    % (self.__class__.__name__, time.time() - self.last_reply_time)
                )
                return
            await self._sock.send(
                self,
                event,
                msg_seq=0,
                wait_for_ack=False,
                pong_icmp_seq=icmp_seq,
                buffer=_pack_msgpack_payload({}),
            )
            self._last_pong_time = time.time()
        else:
            await self._sock.send(
                event,
                msg_seq=0,
                wait_for_ack=False,
                buffer=_pack_msgpack_payload({}),
            )


class SessionStreamManager(object):
    def __init__(self, ping_timeout=PING_TIMEOUT_DEFAULT):
        self._session_streams = []
        self._ping_timeout = ping_timeout

    @property
    def session_streams(self):
        return self._session_streams

    @property
    def pending_auth_count(self):
        """Number of sessions waiting for the challenge response."""
        return len(
            [
                it
                for it in self._session_streams
                if it.status == ICMPTransportSocket.STATUS_AUTHENTICATING
            ]
        )

    def get_session_stream(self, client_address, server_address):
        for stream in self._session_streams:
            if (
                stream.client_address == client_address
                and stream.server_address[1] == server_address[1]
            ):
                return stream
        return None

    def on_new_session(self, sock, client_address, server_address, ping_timeout=None, max_data_size=None):
        session_stream = ICMPSessionStream(
            sock,
            client_address,
            server_address,
            ping_timeout=ping_timeout if ping_timeout is not None else self._ping_timeout,
            max_data_size=max_data_size,
        )
        if session_stream in self._session_streams:
            utils.logger.warning(
                "[%s] ICMPSession %s:%d => %s:%d is already exist"
                % (
                    self.__class__.__name__,
                    client_address[0],
                    client_address[1],
                    server_address[0],
                    server_address[1],
                )
            )
            return session_stream, False
        self._session_streams.append(session_stream)
        return session_stream, True

    def remove_session_stream(self, session_stream):
        if session_stream in self._session_streams:
            self._session_streams.remove(session_stream)
            utils.logger.info(
                "[%s] Remove session stream %s:%d => %s:%d"
                % (
                    self.__class__.__name__,
                    session_stream.client_address[0],
                    session_stream.client_address[1],
                    session_stream.server_address[0],
                    session_stream.server_address[1],
                )
            )
            return True
        return False


class ICMPTransportSocket(object):

    MAX_WINDOW_SIZE = 5
    ACK_TIMEOUT = 2
    SEND_TIMEOUT = 60

    PING_INTERVAL = 5
    PING_TIMEOUT = 30
    MAX_IDLE_SLOTS = 500

    STATUS_NOT_CONNECT = 0
    STATUS_CONNECTING = 1
    STATUS_ESTABLISHED = 2
    STATUS_DISCONNECT = 3
    STATUS_AUTHENTICATING = 4

    def __init__(self, server_side=False, max_data_size=None):
        self._sock = AsyncICMPSocket()
        self._server_side = server_side
        if max_data_size:
            self._max_data_size = max(
                ICMPTransportPacket.MIN_DATA_SIZE,
                min(max_data_size, ICMPTransportPacket.MAX_DATA_SIZE),
            )
        else:
            self._max_data_size = ICMPTransportPacket.MAX_DATA_SIZE
        self._sending_buffers = []
        self._unack_packets = 0
        self._last_acks = {}
        self._last_send_times = {}
        self._running = True
        utils.safe_ensure_future(self.send_packets_task())

    @property
    def max_data_size(self):
        return self._max_data_size

    def enqueue_sending_buffers(
        self,
        address,
        msg_seq,
        msg_ack,
        buffer,
        icmp_type,
        icmp_code,
        icmp_ident=None,
        icmp_seq=None,
        wait_for_ack=True,
        priority=0,
    ):
        self._sending_buffers.append(
            {
                "address": address,
                "msg_seq": msg_seq,
                "timestamp": 0,
                "buffer": buffer,
                "icmp_type": icmp_type,
                "icmp_code": icmp_code,
                "icmp_ident": icmp_ident,
                "icmp_seq": icmp_seq,
                "wait_for_ack": wait_for_ack,
                "count": 0,
                "priority": priority,
            }
        )
        if len(self._sending_buffers) >= self.__class__.MAX_WINDOW_SIZE:
            utils.logger.info(
                "[%s] There is %d messages in sending buffers"
                % (self.__class__.__name__, len(self._sending_buffers))
            )

    def on_ack_received(self, address, ack_num):
        if ack_num <= 0:
            return

        ack_matched = False
        delete_list = []
        for message in self._sending_buffers:
            if message["address"] != address or message["msg_seq"] == 0:
                continue

            if message["msg_seq"] <= ack_num:
                # message is received by peer
                delete_list.append(message)
                self._unack_packets -= 1
                if message["count"] > 1:
                    log = (
                        "[%s] Received ack of message %d from %s:%d, tried %d times"
                        % (
                            self.__class__.__name__,
                            message["msg_seq"],
                            address[0],
                            address[1],
                            message["count"],
                        )
                    )
                    utils.logger.debug(log)
                ack_matched = True

        if not ack_matched:
            utils.logger.verbose(
                "[%s] Ignore ack of message %d from %s:%d"
                % (self.__class__.__name__, ack_num, address[0], address[1])
            )
        if delete_list:
            if any(it["count"] == 0 for it in delete_list):
                utils.logger.info(
                    "[%s] Ignore send messages: %s"
                    % (
                        self.__class__.__name__,
                        ", ".join([str(it["msg_seq"]) for it in delete_list]),
                    )
                )
            for it in delete_list:
                self._sending_buffers.remove(it)

    def drop_pending_packets(self, address):
        """Drop every queued packet for a peer that just went away.

        send_packets_task retransmits whatever is left in _sending_buffers, so
        a client that aborts the handshake (e.g. it answers the challenge with
        OP_CLOSE) would keep its unanswered CHAL retransmitted forever, even
        after the session itself was already removed.
        """
        delete_list = [
            it
            for it in self._sending_buffers
            if it["address"] == address and it["msg_seq"] != 0
        ]
        for it in delete_list:
            self._sending_buffers.remove(it)
            self._unack_packets = max(0, self._unack_packets - 1)

    async def wait_for_ack(self, address, msg_seq, timeout=None):
        time0 = time.time()
        while not timeout or time.time() - time0 < timeout:
            for it in self._sending_buffers:
                if it["address"] == address and it["msg_seq"] == msg_seq:
                    break
            else:
                # message not found in sending buffers
                return True
            await asyncio.sleep(0.005)
        else:
            utils.logger.error(
                "[%s] Waiting for ack of message %d from %s:%d timeout"
                % (self.__class__.__name__, msg_seq, address[0], address[1])
            )
            return False

    async def send_packets_task(self):
        send_times = {}
        min_send_interval = 0.005
        while self._running or self._sending_buffers:
            if not self._sending_buffers:
                await asyncio.sleep(0.005)

            for message in self._sending_buffers:
                if not self._running and message["wait_for_ack"]:
                    # Socket is tearing down: stop retransmitting stream data
                    # that will never be acked.
                    self._sending_buffers.remove(message)
                    break
                if (
                    message["priority"] == 0
                    and message["count"] == 0
                    and self._unack_packets >= self.__class__.MAX_WINDOW_SIZE
                ):
                    # Retransmissions are never blocked by the window, otherwise
                    # a full window with a silent peer would freeze the connection.
                    continue
                if (
                    message["wait_for_ack"]
                    and time.time() - message["timestamp"] < self.__class__.ACK_TIMEOUT
                ):
                    continue
                if message["address"][0] not in send_times:
                    send_times[message["address"][0]] = 0
                if time.time() - send_times[message["address"][0]] < min_send_interval:
                    continue
                if message["count"] > 0:
                    utils.logger.info(
                        "[%s] Resend message %d to %s, count=%d"
                        % (
                            self.__class__.__name__,
                            message["msg_seq"],
                            message["address"][0],
                            message["count"],
                        )
                    )
                self._sock.sendto(
                    message["buffer"],
                    message["address"][0],
                    type=message["icmp_type"],
                    code=message["icmp_code"],
                    ident=message["icmp_ident"],
                    seq=message["icmp_seq"],
                )
                send_times[message["address"][0]] = time.time()
                if message["msg_seq"] and message["wait_for_ack"]:
                    self._last_send_times[message["address"]] = message[
                        "timestamp"
                    ] = time.time()
                    if message["count"] == 0:
                        # Only the first transmission occupies the send window;
                        # counting retransmissions would inflate it permanently.
                        self._unack_packets += 1
                    message["count"] += 1
                else:
                    self._sending_buffers.remove(message)
                    break
            await asyncio.sleep(0.005)

    async def send(
        self,
        address,
        msg_seq,
        msg_ack,
        event,
        client_port,
        server_port,
        buffer,
        icmp_ident=None,
        icmp_seq=None,
        wait_for_ack=True,
    ):
        utils.logger.debug(
            "[%s] Send %s message to %s:%d, seq=%d, ack=%d, length=%d, icmp_ident=%s, icmp_seq=%s"
            % (
                self.__class__.__name__,
                ICMPTransportPacket.format_op(event),
                address,
                client_port if self._server_side else server_port,
                msg_seq,
                msg_ack,
                len(buffer),
                icmp_ident,
                icmp_seq,
            )
        )

        trans_packet = ICMPTransportPacket(
            msg_seq, msg_ack, event, client_port, server_port, buffer,
            direction=ICMPTransportPacket.DIRECTION_SERVER_TO_CLIENT
            if self._server_side
            else ICMPTransportPacket.DIRECTION_CLIENT_TO_SERVER,
        )
        buffer = trans_packet.serialize()
        icmp_type = (
            EnumICMPType.ECHO if not self._server_side else EnumICMPType.ECHO_REPLY
        )
        icmp_code = EnumICMPEchoCode.NO_CODE
        address = (address, client_port if self._server_side else server_port)
        priority = 0
        if event == ICMPTransportPacket.OP_PING:
            priority = 10
        self.enqueue_sending_buffers(
            address,
            msg_seq,
            msg_ack,
            buffer,
            icmp_type,
            icmp_code,
            icmp_ident,
            icmp_seq,
            wait_for_ack=wait_for_ack,
            priority=priority,
        )

        while (
            priority == 0
            and len(self._sending_buffers) >= self.__class__.MAX_WINDOW_SIZE
        ):
            await asyncio.sleep(0.1)

        if msg_seq != 0 and wait_for_ack:
            utils.safe_ensure_future(
                self.wait_for_ack(address, msg_seq, self.__class__.SEND_TIMEOUT)
            )


class ICMPTransportClientSocket(ICMPTransportSocket):
    """ICMP Transport CLient Socket"""

    MAX_PING_CONCURRENCY = 3
    MAX_SENT_REQUESTS = 8192
    # How long _send_close waits for the OP_CLOSE to actually leave the
    # sending queue before giving up.
    CLOSE_SEND_TIMEOUT = 0.5

    def __init__(self, max_data_size=None, auth=None):
        super(ICMPTransportClientSocket, self).__init__(
            server_side=False, max_data_size=max_data_size
        )
        self._auth = auth
        self._local_address = None
        self._remote_address = None
        self._session_stream = None
        self._next_seq = 1
        self._status = ICMPTransportSocket.STATUS_NOT_CONNECT
        self._last_pong_time = 0
        self._current_pings = {}
        # ICMP echo identifier is fixed per session so that NAT devices keep a
        # single conntrack entry; the sequence number increments per packet.
        self._icmp_ident = random.randint(1, 65535)
        self._icmp_seq = 0
        self._sent_requests = {}
        # Per-connection PONG delay negotiated with the server during CONNECT.
        # None means "do not negotiate"; the server then keeps its own default.
        self._ping_timeout = None

    @property
    def session_stream(self):
        return self._session_stream

    @property
    def status(self):
        return self._status

    def _record_sent_request(self, seq):
        """Remember requests we sent so replies can be matched by echo value."""
        now = time.time()
        self._sent_requests[seq] = now
        if len(self._sent_requests) > self.__class__.MAX_SENT_REQUESTS:
            expire_time = ICMPTransportSocket.SEND_TIMEOUT
            self._sent_requests = {
                k: v
                for k, v in self._sent_requests.items()
                if now - v < expire_time
            }

    def _is_valid_echo(self, icmp_packet):
        """Verify the reply echoes a request we actually sent.

        NOTE: this only blocks blind forgeries. Both ident and seq travel in
        cleartext inside the ICMP header, so an on-path sniffer can replay or
        craft matching values. This is not a substitute for payload encryption.
        """
        if icmp_packet.ident != self._icmp_ident:
            utils.logger.warning(
                "[%s] Drop reply with unexpected ident %d, expect %d"
                % (self.__class__.__name__, icmp_packet.ident, self._icmp_ident)
            )
            return False
        if icmp_packet.sequence not in self._sent_requests:
            utils.logger.warning(
                "[%s] Drop reply with unknown sequence %d"
                % (self.__class__.__name__, icmp_packet.sequence)
            )
            return False
        return True

    async def send(self, event, buffer=b"", msg_seq=None, wait_for_ack=True):
        assert self._session_stream is not None
        assert self._status != ICMPTransportSocket.STATUS_DISCONNECT
        assert msg_seq is None or isinstance(msg_seq, int)
        if msg_seq is None:
            msg_seq = self._session_stream.next_seq
        ack_num = self._session_stream.next_ack
        self._session_stream.on_send_ack(ack_num)
        # Sequence cycles in [1, 65535] and never takes 0. A fixed ident plus an
        # incrementing seq keeps the whole session on one NAT conntrack entry.
        self._icmp_seq = self._icmp_seq % 65535 + 1
        self._record_sent_request(self._icmp_seq)
        if event == ICMPTransportPacket.OP_PING:
            self._current_pings[self._icmp_seq] = time.time()
        return await super(ICMPTransportClientSocket, self).send(
            self._remote_address[0],
            msg_seq,
            ack_num,
            event,
            self._local_address[1],
            self._remote_address[1],
            buffer,
            icmp_ident=self._icmp_ident,
            icmp_seq=self._icmp_seq,
            wait_for_ack=wait_for_ack,
        )

    async def send_message(self, buffer):
        max_size = self._session_stream.max_data_size
        offset = 0
        if len(buffer) > max_size:
            utils.logger.info(
                "[%s] Send %d bytes big message to %s:%d"
                % (
                    self.__class__.__name__,
                    len(buffer),
                    self._remote_address[0],
                    self._remote_address[1],
                )
            )
        while offset < len(buffer):
            await self.send(
                ICMPTransportPacket.OP_DATA,
                buffer[offset : offset + max_size],
            )
            offset += max_size

    async def handling_message_task(self, address):
        icmp_type = EnumICMPType.ECHO_REPLY
        icmp_code = EnumICMPEchoCode.NO_CODE
        while self._status != ICMPTransportSocket.STATUS_DISCONNECT:
            addr, icmp_packet = await self._sock.recvfrom(
                address=address, type=icmp_type, code=icmp_code
            )
            # Build the raw-recv line once; it is only logged for packets we end
            # up dropping (unexpected), so the expected path prints just the
            # single "Received ... message" line below instead of two.
            raw_log = "[%s] RAW type=0 recv from %s, ident=%d, seq=%d, length=%d" % (
                self.__class__.__name__,
                addr,
                icmp_packet.ident,
                icmp_packet.sequence,
                len(icmp_packet.data),
            )
            if not self._is_valid_echo(icmp_packet):
                utils.logger.info("%s (dropped: not a valid echo of a request we sent)" % raw_log)
                continue
            buffer = icmp_packet.data
            if not buffer.startswith(ICMPTransportPacket.MAGIC_FLAG):
                utils.logger.info("%s (dropped: missing MAGIC_FLAG, length=%d)" % (raw_log, len(buffer)))
                continue

            try:
                trans_packet = ICMPTransportPacket.unserialize_from(buffer)
            except utils.TunnelPacketError as ex:
                utils.logger.warning("%s (dropped: unserialize failed: %s)" % (raw_log, ex))
                continue

            # Drop our own kernel-generated echo: it carries the direction bit we
            # sent (C->S), i.e. it is the echo of a request we issued, not a
            # response from the server.
            if trans_packet.direction == ICMPTransportPacket.DIRECTION_CLIENT_TO_SERVER:
                utils.logger.info(
                    "%s (dropped: reflected client->server echo / kernel echo of our own request, seq=%d, direction=%d, event=%s)"
                    % (
                        raw_log,
                        icmp_packet.sequence,
                        trans_packet.direction,
                        ICMPTransportPacket.format_op(trans_packet.event),
                    )
                )
                continue

            if trans_packet.server_port != self._remote_address[1]:
                utils.logger.warning(
                    "%s (dropped: server port mismatch: %d vs %d)"
                    % (
                        raw_log,
                        self._remote_address[1],
                        trans_packet.server_port,
                    )
                )
                continue

            message = "[%s] Received %s message from %s:%d, seq=%d, ack=%d, length=%d, icmp_ident=%d, icmp_seq=%d" % (
                self.__class__.__name__,
                ICMPTransportPacket.format_op(trans_packet.event),
                addr,
                trans_packet.server_port,
                trans_packet.seq_num,
                trans_packet.ack_num,
                len(trans_packet.padding),
                icmp_packet.ident,
                icmp_packet.sequence,
            )
            if (
                trans_packet.event == ICMPTransportPacket.OP_PONG
                and icmp_packet.sequence in self._current_pings
            ):
                request_time = self._current_pings.pop(icmp_packet.sequence)
                message += ", ping_time=%.2fs" % (time.time() - request_time)

            utils.logger.debug(message)

            self._session_stream.on_message_received(trans_packet)

            if self._status in (
                ICMPTransportSocket.STATUS_NOT_CONNECT,
                ICMPTransportSocket.STATUS_DISCONNECT,
            ):
                utils.logger.error(
                    "[%s] Received packet event=%s when connection status is %d"
                    % (
                        self.__class__.__name__,
                        ICMPTransportPacket.format_op(trans_packet.event),
                        self._status,
                    )
                )
                continue
            elif self._status == ICMPTransportSocket.STATUS_CONNECTING:
                if trans_packet.opcode == ICMPTransportPacket.OP_OK:
                    self._status = ICMPTransportSocket.STATUS_ESTABLISHED
                    utils.logger.info(
                        "[%s] Connection %s:%d => %s:%d established"
                        % (
                            self.__class__.__name__,
                            self._local_address[0] or "0.0.0.0",
                            self._local_address[1],
                            self._remote_address[0],
                            self._remote_address[1],
                        )
                    )
                    self._session_stream.on_data_received(trans_packet.seq_num, b"")
                elif trans_packet.opcode == ICMPTransportPacket.OP_FAIL:
                    self._status = ICMPTransportSocket.STATUS_DISCONNECT
                elif trans_packet.event == ICMPTransportPacket.OP_CHAL:
                    self._session_stream.on_data_received(trans_packet.seq_num, b"")
                    await self.on_challenge_received(trans_packet)
                else:
                    utils.logger.error(
                        "[%s] Unexpected packet event=%s when connecting"
                        % (self.__class__.__name__, ICMPTransportPacket.format_op(trans_packet.event))
                    )
                    continue
            elif self._status == ICMPTransportSocket.STATUS_AUTHENTICATING:
                if trans_packet.opcode == ICMPTransportPacket.OP_OK:
                    self._status = ICMPTransportSocket.STATUS_ESTABLISHED
                    utils.logger.info(
                        "[%s] Connection %s:%d => %s:%d established"
                        % (
                            self.__class__.__name__,
                            self._local_address[0] or "0.0.0.0",
                            self._local_address[1],
                            self._remote_address[0],
                            self._remote_address[1],
                        )
                    )
                    self._session_stream.on_data_received(trans_packet.seq_num, b"")
                elif trans_packet.opcode == ICMPTransportPacket.OP_FAIL:
                    self._status = ICMPTransportSocket.STATUS_DISCONNECT
                else:
                    utils.logger.error(
                        "[%s] Unexpected packet event=%s when authenticating"
                        % (self.__class__.__name__, ICMPTransportPacket.format_op(trans_packet.event))
                    )
                    continue
            elif self._status == ICMPTransportSocket.STATUS_ESTABLISHED:
                if trans_packet.event == ICMPTransportPacket.OP_RESET:
                    utils.logger.warning(
                        "[%s] Connection %s:%d => %s:%d received RESET event"
                        % (
                            self.__class__.__name__,
                            self._local_address[0] or "0.0.0.0",
                            self._local_address[1],
                            self._remote_address[0],
                            self._remote_address[1],
                        )
                    )
                    self._session_stream.reset()
                    self._status = ICMPTransportSocket.STATUS_DISCONNECT
                elif trans_packet.event == ICMPTransportPacket.OP_DATA:
                    self._session_stream.on_data_received(
                        trans_packet.seq_num, trans_packet.padding
                    )
                    # A downlink packet is itself proof the server is alive and
                    # able to reply, so it satisfies any outstanding keepalive
                    # pings just like a PONG would (the server may skip the PONG
                    # when it has already replied with data). Treat it as a PONG
                    # answer to avoid spurious "Ping request N timeout" logs.
                    self._last_pong_time = time.time()
                    self._current_pings.clear()
                elif trans_packet.event == ICMPTransportPacket.OP_CLOSE:
                    self._session_stream.reset()
                    self._status = ICMPTransportSocket.STATUS_DISCONNECT
                elif trans_packet.event == ICMPTransportPacket.OP_PONG:
                    self._last_pong_time = time.time()
                    utils.logger.info(
                        "[%s] OP_PONG received it=%d, direction=%d, icmp_seq=%d"
                        % (
                            self.__class__.__name__,
                            trans_packet.seq_num,
                            trans_packet.direction,
                            icmp_packet.sequence,
                        )
                    )
                else:
                    utils.logger.warning(
                        "[%s] Unexpected packet event=%s when connection established"
                        % (self.__class__.__name__, ICMPTransportPacket.format_op(trans_packet.event))
                    )
                    continue
        self._running = False
        utils.logger.info("[%s] Handling message task exit" % self.__class__.__name__)

    async def on_challenge_received(self, trans_packet):
        """Answer the server challenge with HMAC-SHA256(password, nonce)."""
        try:
            msg = _unpack_msgpack_payload(trans_packet.padding)
            if not isinstance(msg, dict):
                raise utils.TunnelPacketError("challenge payload is not a dict")
            nonce = msg.get("nonce")
        except utils.TunnelPacketError as ex:
            utils.logger.error(
                "[%s] Malformed challenge from server: %s"
                % (self.__class__.__name__, ex)
            )
            await self._send_close()
            self._status = ICMPTransportSocket.STATUS_DISCONNECT
            return
        if isinstance(nonce, str):
            nonce = nonce.encode()
        if not self._auth or len(self._auth) < 2 or not self._auth[1]:
            utils.logger.error(
                "[%s] Server requires authentication but no password is configured"
                % self.__class__.__name__
            )
            await self._send_close()
            self._status = ICMPTransportSocket.STATUS_DISCONNECT
            return
        if not nonce:
            utils.logger.error(
                "[%s] Received empty challenge from server" % self.__class__.__name__
            )
            await self._send_close()
            self._status = ICMPTransportSocket.STATUS_DISCONNECT
            return
        response = hmac.new(self._auth[1].encode(), nonce, hashlib.sha256).digest()
        self._status = ICMPTransportSocket.STATUS_AUTHENTICATING
        await self.send(
            ICMPTransportPacket.OP_AUTH,
            buffer=_pack_msgpack_payload({"response": response}),
        )

    async def _send_close(self):
        """Best-effort notify the server that this side is aborting the handshake,
        so it can drop the pending-auth session instead of waiting for the timeout.

        send() only enqueues the packet; send_packets_task performs the real
        sendto. We are about to tear the socket down (which stops that task and
        may end the process right away), so wait here until this exact message
        has left the queue - otherwise the OP_CLOSE is dropped before it ever
        reaches the wire.
        """
        try:
            msg_seq = self._session_stream.next_seq
            await self.send(
                ICMPTransportPacket.OP_CLOSE,
                buffer=_pack_msgpack_payload({}),
                msg_seq=msg_seq,
                wait_for_ack=False,
            )
            time0 = time.time()
            while time.time() - time0 < self.__class__.CLOSE_SEND_TIMEOUT:
                if all(
                    it["msg_seq"] != msg_seq or it["wait_for_ack"]
                    for it in self._sending_buffers
                ):
                    return
                await asyncio.sleep(0.005)
            utils.logger.warning(
                "[%s] OP_CLOSE still unsent after %.2fs"
                % (self.__class__.__name__, self.__class__.CLOSE_SEND_TIMEOUT)
            )
        except Exception as ex:
            utils.logger.warning(
                "[%s] Failed to send OP_CLOSE to %s:%d: %s"
                % (
                    self.__class__.__name__,
                    self._remote_address[0] if self._remote_address else "?",
                    self._remote_address[1] if self._remote_address else 0,
                    ex,
                )
            )

    async def connect(self, address, timeout=10):
        assert self._remote_address is None
        await self._sock.start()
        self._remote_address = address
        self._local_address = (None, random.randint(0, 65535))  # FIXME
        self._session_stream = ICMPSessionStream(
            self,
            self._local_address,
            self._remote_address,
            server_side=False,
            max_data_size=self._max_data_size,
        )
        utils.safe_ensure_future(self.handling_message_task(self._remote_address[0]))
        self._status = ICMPTransportSocket.STATUS_CONNECTING
        last_send_time = time0 = time.time()
        username = self._auth[0] if self._auth else ""
        # The CONNECT payload carries per-session parameters as a plain msgpack
        # dict (keys: username, ping_timeout, mss). The server applies them to
        # this session (PONG delay and max packet payload).
        connect_msg = {"username": username}
        if self._ping_timeout:
            connect_msg["ping_timeout"] = self._ping_timeout
        if self._max_data_size:
            connect_msg["mss"] = self._max_data_size
        connect_buffer = _pack_msgpack_payload(connect_msg)
        await self.send(
            ICMPTransportPacket.OP_CONNECT, buffer=connect_buffer
        )
        while time.time() - time0 < timeout:
            if self._status == ICMPTransportSocket.STATUS_ESTABLISHED:
                utils.safe_ensure_future(self.ping_task())
                return True
            elif self._status == ICMPTransportSocket.STATUS_DISCONNECT:
                return False
            # Only resend while still connecting; once the challenge arrived the
            # server has a session for us and a duplicate CNCT would be rejected.
            if (
                self._status == ICMPTransportSocket.STATUS_CONNECTING
                and time.time() - last_send_time >= 1
            ):
                await self.send(
                    ICMPTransportPacket.OP_CONNECT, buffer=connect_buffer
                )
                last_send_time = time.time()
            await asyncio.sleep(0.005)
        else:
            return False

    async def ping_task(self):
        min_ping_concurrency = 1
        ping_concurrency = min_ping_concurrency
        last_ping_time = 0
        while self._status == ICMPTransportSocket.STATUS_ESTABLISHED:
            idle_time = time.time() - self._session_stream.last_alive_time
            if idle_time >= self.__class__.PING_TIMEOUT * 3:
                # connection timeout
                await self._send_close()
                self._status = ICMPTransportSocket.STATUS_DISCONNECT
                utils.logger.error(
                    "[%s] Connection %s:%d => %s:%d ping timeout: %.2fs"
                    % (
                        self.__class__.__name__,
                        self._local_address[0] or "0.0.0.0",
                        self._local_address[1],
                        self._remote_address[0],
                        self._remote_address[1],
                        time.time() - self._last_pong_time,
                    )
                )
                return

            for it in self._current_pings:
                if (
                    time.time() - self._current_pings[it]
                    > self.__class__.PING_INTERVAL + 0.5
                ):
                    utils.logger.info(
                        "[%s] Ping request %d timeout" % (self.__class__.__name__, it)
                    )
                    self._current_pings.pop(it)
                    break

            waiting_time = time.time() - self._session_stream.last_data_time
            if waiting_time < 0.2:
                if ping_concurrency < self.__class__.MAX_PING_CONCURRENCY:
                    ping_concurrency += 1
                    utils.logger.debug(
                        "[%s] Ping concurrency changed to %d"
                        % (self.__class__.__name__, ping_concurrency)
                    )
            elif waiting_time > 0.5:
                if ping_concurrency > min_ping_concurrency:
                    ping_concurrency -= 1
                    utils.logger.debug(
                        "[%s] Ping concurrency changed to %d"
                        % (self.__class__.__name__, ping_concurrency)
                    )

            current_pings = self._current_pings
            if len(current_pings) < ping_concurrency:
                # While idle, throttle keepalive PINGs to one per PING_INTERVAL
                # instead of firing them back-to-back. PING_INTERVAL lines up with
                # the peer's response-slot expiry window (PING_INTERVAL + 1), so a
                # PING every PING_INTERVAL always leaves at least one response slot
                # alive on the server, letting it push downlink data without first
                # waiting for the client to send a fresh request.
                if (
                    waiting_time > 0.5
                    and time.time() - last_ping_time < self.__class__.PING_INTERVAL
                ):
                    pass
                else:
                    for _ in range(ping_concurrency - len(current_pings)):
                        await self.send(
                            ICMPTransportPacket.OP_PING,
                            msg_seq=0,
                            wait_for_ack=False,
                            buffer=_pack_msgpack_payload({}),
                        )
                        last_ping_time = time.time()

            await asyncio.sleep(0.1)
        utils.logger.info(
            "[%s] Ping task exit, current status is %d"
            % (self.__class__.__name__, self._status)
        )

    async def recv(self, timeout=None):
        return await self._session_stream.read()


class ICMPTunnelStreamHandler(object):
    async def handle_session_stream(self, session_stream):
        pass


class ICMPTransportServerSocket(ICMPTransportSocket):
    """ICMP Transport Server Socket"""

    NONCE_SIZE = 32
    AUTH_TIMEOUT = 30
    MAX_PENDING_AUTH = 64

    def __init__(
        self,
        stream_handler_cls,
        max_data_size=None,
        auth=None,
        ping_timeout=PING_TIMEOUT_DEFAULT,
    ):
        super(ICMPTransportServerSocket, self).__init__(
            True, max_data_size=max_data_size
        )
        self._stream_handler_cls = stream_handler_cls
        self._listen_address = None
        self._auth = auth
        self._ping_timeout = ping_timeout
        self._sessmgr = SessionStreamManager(ping_timeout=ping_timeout)
        utils.safe_ensure_future(self.clean_pending_auth_task())

    async def handle_new_stream(self, session_stream):
        await self.send(
            session_stream,
            ICMPTransportPacket.OP_OK,
            buffer=_pack_msgpack_payload({}),
        )
        await self._stream_handler_cls().handle_session_stream(session_stream)

    async def handle_auth(self, session_stream, trans_packet):
        """Verify the HMAC response to the challenge we sent."""
        address = session_stream.client_address
        if not self._auth or not session_stream.auth_nonce:
            utils.logger.warning(
                "[%s] Unexpected AUTH message from %s:%d"
                % (self.__class__.__name__, address[0], address[1])
            )
            return
        expected = hmac.new(
            self._auth[1].encode(), session_stream.auth_nonce, hashlib.sha256
        ).digest()
        try:
            msg = _unpack_msgpack_payload(trans_packet.padding)
            if not isinstance(msg, dict):
                raise utils.TunnelPacketError("auth payload is not a dict")
            response = msg.get("response")
        except utils.TunnelPacketError as ex:
            response = None
            utils.logger.warning(
                "[%s] Malformed AUTH from %s:%d: %s"
                % (self.__class__.__name__, address[0], address[1], ex)
            )
        if isinstance(response, str):
            response = response.encode()
        if not isinstance(response, bytes) or not hmac.compare_digest(response, expected):
            utils.logger.warning(
                "[%s] Authentication failed for %s:%d"
                % (self.__class__.__name__, address[0], address[1])
            )
            await self.send(
                session_stream,
                ICMPTransportPacket.OP_FAIL,
                buffer=_pack_msgpack_payload({}),
            )
            session_stream.status = ICMPTransportSocket.STATUS_DISCONNECT
            self._sessmgr.remove_session_stream(session_stream)
            return
        session_stream.auth_nonce = None
        utils.logger.info(
            "[%s] Client %s:%d authenticated"
            % (self.__class__.__name__, address[0], address[1])
        )
        utils.safe_ensure_future(self.handle_new_stream(session_stream))
        session_stream.status = ICMPTransportSocket.STATUS_ESTABLISHED

    async def clean_pending_auth_task(self):
        """Drop sessions that never completed the challenge-response."""
        while True:
            await asyncio.sleep(self.__class__.AUTH_TIMEOUT / 2)
            for session_stream in list(self._sessmgr.session_streams):
                if (
                    session_stream.status == ICMPTransportSocket.STATUS_AUTHENTICATING
                    and time.time() - session_stream.auth_time
                    >= self.__class__.AUTH_TIMEOUT
                ):
                    utils.logger.warning(
                        "[%s] Authentication timeout for %s:%d"
                        % (
                            self.__class__.__name__,
                            session_stream.client_address[0],
                            session_stream.client_address[1],
                        )
                    )
                    self._sessmgr.remove_session_stream(session_stream)

    async def send(
        self,
        session_stream,
        event,
        buffer=b"",
        msg_seq=None,
        wait_for_ack=True,
        pong_icmp_seq=None,
    ):
        if msg_seq is None:
            msg_seq = session_stream.next_seq
        ack_num = session_stream.next_ack
        session_stream.on_send_ack(ack_num)
        if event == ICMPTransportPacket.OP_PONG:
            # PONG is a keepalive control packet and must NOT consume a response
            # slot. Downlink data already drains the pool, so if PONG competed
            # for it the PONG would be silently dropped whenever slots ran out,
            # causing the client's ping to time out. Reuse the most recent
            # client icmp ident (cached on the session) for the reply, and use
            # the PING's own icmp_seq (pong_icmp_seq) so the client can correlate
            # each pending ping. Fall back to a real slot only if we have no
            # client history at all.
            client_icmp = session_stream.last_client_icmp
            if client_icmp is None:
                ident, seq = await session_stream.get_response_slot()
            else:
                ident = client_icmp[0]
                seq = (
                    pong_icmp_seq
                    if pong_icmp_seq is not None
                    else client_icmp[1]
                )
        else:
            ident, seq = await session_stream.get_response_slot()
        result = await super(ICMPTransportServerSocket, self).send(
            session_stream.client_address[0],
            msg_seq,
            ack_num,
            event,
            session_stream.client_address[1],
            session_stream.server_address[1],
            buffer,
            icmp_ident=ident,
            icmp_seq=seq,
            wait_for_ack=wait_for_ack,
        )
        # Any reply we send to the client (downlink data, PONG, ...) proves the
        # channel is alive. The PONG path uses this to skip a redundant keepalive
        # PONG when we've already replied with data in the pong window.
        session_stream.last_reply_time = time.time()
        return result

    async def send_message(self, session_stream, buffer):
        max_size = session_stream.max_data_size
        offset = 0
        if len(buffer) > max_size:
            utils.logger.info(
                "[%s] Send %d bytes big message to %s:%d"
                % (
                    self.__class__.__name__,
                    len(buffer),
                    session_stream.client_address[0],
                    session_stream.client_address[1],
                )
            )
        while offset < len(buffer):
            await self.send(
                session_stream,
                ICMPTransportPacket.OP_DATA,
                buffer[offset : offset + max_size],
            )
            offset += max_size

    async def send_reset(self, client_address, server_address, ident, seq):
        return await super(ICMPTransportServerSocket, self).send(
            client_address[0],
            0,
            0,
            ICMPTransportPacket.OP_RESET,
            client_address[1],
            server_address[1],
            _pack_msgpack_payload({}),
            icmp_ident=ident,
            icmp_seq=seq,
            wait_for_ack=False,
        )

    async def check_and_add_response_slot(
        self, session_stream, client_address, icmp_packet
    ):
        """Clean expired slots and enforce the idle slot limit.

        Returns True if the packet should be processed further, False if a
        RESET has been sent and the packet must be dropped.
        """
        session_stream.clean_expired_slots()
        slot_count = session_stream.slot_count
        if slot_count >= self.__class__.MAX_IDLE_SLOTS:
            utils.logger.warning(
                "[%s][%s:%d] Too many idle response slots: %d >= %d, reset connection"
                % (
                    self.__class__.__name__,
                    client_address[0],
                    client_address[1],
                    slot_count,
                    self.__class__.MAX_IDLE_SLOTS,
                )
            )
            await self.send_reset(
                client_address,
                self._listen_address,
                icmp_packet.ident,
                icmp_packet.sequence,
            )
            session_stream.clear_response_slots()
            self._sessmgr.remove_session_stream(session_stream)
            return False
        session_stream.add_response_slot(icmp_packet.ident, icmp_packet.sequence)
        return True

    async def listen(self, address):
        assert self._listen_address is None
        await self._sock.start()
        self._listen_address = address

        icmp_type = EnumICMPType.ECHO
        icmp_code = EnumICMPEchoCode.NO_CODE
        while True:
            addr, icmp_packet = await self._sock.recvfrom(
                address=None, type=icmp_type, code=icmp_code
            )
            buffer = icmp_packet.data
            utils.logger.info(
                "[%s] RAW type=8 recv from %s, ident=%d, seq=%d, magic=%s"
                % (
                    self.__class__.__name__,
                    addr,
                    icmp_packet.ident,
                    icmp_packet.sequence,
                    buffer.startswith(ICMPTransportPacket.MAGIC_FLAG),
                )
            )
            if not buffer.startswith(ICMPTransportPacket.MAGIC_FLAG):
                # send pong to client
                self._sock.sendto(
                    buffer,
                    addr,
                    type=EnumICMPType.ECHO_REPLY,
                    code=icmp_code,
                    ident=icmp_packet.ident,
                    seq=icmp_packet.sequence,
                )
                continue

            try:
                trans_packet = ICMPTransportPacket.unserialize_from(buffer)
            except utils.TunnelPacketError as ex:
                utils.logger.warning(str(ex))
                continue
            # Drop our own kernel-generated echo of a server->client response.
            if trans_packet.direction == ICMPTransportPacket.DIRECTION_SERVER_TO_CLIENT:
                utils.logger.debug(
                    "[%s] Drop server->client reflected packet ident=%d seq=%d"
                    % (self.__class__.__name__, icmp_packet.ident, icmp_packet.sequence)
                )
                continue
            if trans_packet.server_port != self._listen_address[1]:
                utils.logger.warning(
                    "[%s] Server port mismatch: %d vs %d"
                    % (
                        self.__class__.__name__,
                        self._listen_address[1],
                        trans_packet.server_port,
                    )
                )
                continue

            utils.logger.debug(
                "[%s] Received %s message from %s:%d, seq=%d, ack=%d, length=%d, icmp_ident=%d, icmp_seq=%d"
                % (
                    self.__class__.__name__,
                    ICMPTransportPacket.format_op(trans_packet.event),
                    addr,
                    trans_packet.client_port,
                    trans_packet.seq_num,
                    trans_packet.ack_num,
                    len(trans_packet.padding),
                    icmp_packet.ident,
                    icmp_packet.sequence,
                )
            )

            if trans_packet.event == ICMPTransportPacket.OP_CONNECT:
                utils.logger.info(
                    "[%s] Received OP_CONNECT from %s:%d"
                    % (self.__class__.__name__, addr, trans_packet.client_port)
                )
                # Parse per-session parameters from the CONNECT payload, a plain
                # msgpack dict (keys: username, ping_timeout, mss). The server
                # applies them to this session (PONG delay and max packet
                # payload).
                client_ping_timeout = None
                client_mss = None
                try:
                    connect_msg = _unpack_msgpack_payload(trans_packet.padding)
                except utils.TunnelPacketError as ex:
                    utils.logger.warning(
                        "[%s] Invalid CONNECT payload from %s:%d: %s"
                        % (self.__class__.__name__, addr, trans_packet.client_port, ex)
                    )
                    continue
                if not isinstance(connect_msg, dict):
                    utils.logger.warning(
                        "[%s] Invalid CONNECT payload from %s:%d: not a dict"
                        % (self.__class__.__name__, addr, trans_packet.client_port)
                    )
                    continue
                username = connect_msg.get("username") or b""
                if isinstance(username, bytes):
                    username = username.decode()
                client_ping_timeout = connect_msg.get("ping_timeout")
                client_mss = connect_msg.get("mss")
                eff_ping_timeout = (
                    client_ping_timeout
                    if client_ping_timeout is not None
                    else (self._ping_timeout or PING_TIMEOUT_DEFAULT)
                )
                eff_mss = (
                    client_mss if client_mss is not None else self._max_data_size
                )
                utils.logger.info(
                    "[%s] Client %s:%d session params: ping_timeout=%.2fs (%s), mss=%s (%s)"
                    % (
                        self.__class__.__name__,
                        addr,
                        trans_packet.client_port,
                        eff_ping_timeout,
                        "client" if client_ping_timeout is not None else "server-default",
                        eff_mss if eff_mss is not None else "unset",
                        "client" if client_mss is not None else "server-default",
                    )
                )
                session_stream, result = self._sessmgr.on_new_session(
                    self,
                    (addr, trans_packet.client_port),
                    self._listen_address,
                    ping_timeout=client_ping_timeout,
                    max_data_size=client_mss,
                )
                session_stream.on_message_received(trans_packet)
                session_stream.on_data_received(trans_packet.seq_num, b"")
                if not await self.check_and_add_response_slot(
                    session_stream,
                    (addr, trans_packet.client_port),
                    icmp_packet,
                ):
                    continue
                if result:
                    if self._auth and self._auth[1]:
                        # challenge-response authentication is required
                        if (
                            self._sessmgr.pending_auth_count
                            >= self.__class__.MAX_PENDING_AUTH
                        ):
                            utils.logger.warning(
                                "[%s] Too many pending authentications, reject %s:%d"
                                % (
                                    self.__class__.__name__,
                                    addr,
                                    trans_packet.client_port,
                                )
                            )
                            await self.send(
                                session_stream,
                                ICMPTransportPacket.OP_FAIL,
                                buffer=_pack_msgpack_payload({}),
                            )
                            session_stream.status = (
                                ICMPTransportSocket.STATUS_DISCONNECT
                            )
                            self._sessmgr.remove_session_stream(session_stream)
                            continue
                        session_stream.auth_nonce = os.urandom(
                            self.__class__.NONCE_SIZE
                        )
                        session_stream.status = (
                            ICMPTransportSocket.STATUS_AUTHENTICATING
                        )
                        utils.safe_ensure_future(
                            self.send(
                                session_stream,
                                ICMPTransportPacket.OP_CHAL,
                                buffer=_pack_msgpack_payload({"nonce": session_stream.auth_nonce}),
                            )
                        )
                    else:
                        utils.safe_ensure_future(
                            self.handle_new_stream(session_stream)
                        )
                        session_stream.status = ICMPTransportSocket.STATUS_ESTABLISHED
                else:
                    utils.safe_ensure_future(
                        self.send(
                            session_stream,
                            ICMPTransportPacket.OP_FAIL,
                            buffer=_pack_msgpack_payload({}),
                        )
                    )
                    session_stream.status = ICMPTransportSocket.STATUS_DISCONNECT
            else:
                session_stream = self._sessmgr.get_session_stream(
                    (addr, trans_packet.client_port), self._listen_address
                )
                if not session_stream:
                    utils.logger.warning(
                        "[%s] Get session %s:%d => %s:%d failed"
                        % (
                            self.__class__.__name__,
                            addr,
                            trans_packet.client_port,
                            self._listen_address[0],
                            self._listen_address[1],
                        )
                    )
                    await self.send_reset(
                        (addr, trans_packet.client_port),
                        self._listen_address,
                        icmp_packet.ident,
                        icmp_packet.sequence,
                    )
                    continue
                if trans_packet.event in (
                    ICMPTransportPacket.OP_CLOSE,
                    ICMPTransportPacket.OP_FAIL,
                ):
                    # The client is giving up on this handshake (e.g. it has no
                    # password to answer the challenge). Drop the session
                    # immediately instead of letting it linger until the auth
                    # timeout; no response slot is consumed.
                    utils.logger.info(
                        "[%s] Client %s:%d closed/aborted session (event=%s)"
                        % (
                            self.__class__.__name__,
                            addr,
                            trans_packet.client_port,
                            ICMPTransportPacket.format_op(trans_packet.event),
                        )
                    )
                    self._sessmgr.remove_session_stream(session_stream)
                    self.drop_pending_packets((addr, trans_packet.client_port))
                    continue
                session_stream.on_message_received(trans_packet)
                if not await self.check_and_add_response_slot(
                    session_stream,
                    (addr, trans_packet.client_port),
                    icmp_packet,
                ):
                    continue
                if trans_packet.seq_num and trans_packet.event == ICMPTransportPacket.OP_DATA:
                    session_stream.on_data_received(
                        trans_packet.seq_num, trans_packet.padding
                    )
                elif trans_packet.seq_num and trans_packet.event in (
                    ICMPTransportPacket.OP_AUTH,
                    ICMPTransportPacket.OP_CHAL,
                ):
                    # Auth packets carry credentials rather than stream data, but
                    # they still consume a sequence number, so fill the gap.
                    session_stream.on_data_received(trans_packet.seq_num, b"")

                if trans_packet.event == ICMPTransportPacket.OP_PING:
                    utils.logger.info(
                        "[%s] OP_PING from %s:%d ident=%d seq=%d -> reply PONG"
                        % (
                            self.__class__.__name__,
                            addr,
                            trans_packet.client_port,
                            icmp_packet.ident,
                            icmp_packet.sequence,
                        )
                    )
                    # Reply with a PONG after the (per-server) ping_timeout. The
                    # default ping_timeout is PING_INTERVAL (5s) so the server answers
                    # every keepalive PING within one idle window. Some NAT/firewall
                    # networks drop a 5s-delayed ICMP reply; lower it per-server via
                    # the `ping_timeout` URL parameter (formerly `timeout` /
                    # `pong_delay`), e.g. `icmp://0.0.0.0?ping_timeout=0.5`.
                    utils.safe_ensure_future(
                        session_stream.delay_ack(
                            0,
                            session_stream.ping_timeout,
                            icmp_packet.sequence,
                        )
                    )
                elif trans_packet.event == ICMPTransportPacket.OP_DATA:
                    pass
                elif trans_packet.event == ICMPTransportPacket.OP_AUTH:
                    await self.handle_auth(session_stream, trans_packet)
                else:
                    utils.logger.warning(
                        "[%s] Unexpected icmp transport packet, event=%s"
                        % (self.__class__.__name__, ICMPTransportPacket.format_op(trans_packet.event))
                    )


class StreamForwardPacket(object):
    """Stream Forward Packet"""

    EVENT_CREATE = "create"
    EVENT_WRITE = "write"
    EVENT_CLOSE = "close"

    def __init__(self, event, **kwargs):
        self._event = event
        self._kwargs = kwargs

    def __getattr__(self, attr):
        if attr in self._kwargs:
            return self._kwargs[attr]
        else:
            raise AttributeError(attr)

    @property
    def event(self):
        return self._event

    def serialize(self):
        message = {
            "event": self._event,
        }
        message.update(self._kwargs)
        buffer = msgpack.dumps(message)
        return struct.pack("!I", len(buffer)) + buffer

    @staticmethod
    def unserialize_from(buffer):
        if len(buffer) < 5:
            raise utils.TunnelPacketLengthError(
                "Invalid stream forward packet: %r" % buffer
            )
        buffer_len = struct.unpack("!I", buffer[:4])[0]
        if len(buffer) - 4 != buffer_len:
            raise utils.TunnelPacketLengthError(
                "Invalid stream forward packet length: %d vs %d"
                % (buffer_len + 4, len(buffer))
            )

        message = msgpack.loads(buffer[4:])
        if "event" not in message:
            raise utils.TunnelPacketError(
                "Field `event` not found in message: %s" % message
            )
        event = message.pop("event")
        if event == StreamForwardPacket.EVENT_CREATE:
            return CreateStreamPacket(**message)
        elif event == StreamForwardPacket.EVENT_WRITE:
            return WriteStreamPacket(**message)
        elif event == StreamForwardPacket.EVENT_CLOSE:
            return CloseStreamPacket(**message)
        else:
            raise NotImplementedError("Invalid stream event %s" % event)


class CreateStreamPacket(StreamForwardPacket):
    """Create Stream Packet"""

    def __init__(self, target_address, result=None):
        kwargs = {"target_address": target_address}
        if result is not None:
            kwargs["result"] = result
        super(CreateStreamPacket, self).__init__(self.__class__.EVENT_CREATE, **kwargs)

    @property
    def target_address(self):
        return self._kwargs["target_address"]

    @property
    def result(self):
        return self._kwargs.get("result")

    @result.setter
    def result(self, value):
        self._kwargs["result"] = value


class WriteStreamPacket(StreamForwardPacket):
    """Write Stream Packet"""

    def __init__(self, stream_id, buffer):
        super(WriteStreamPacket, self).__init__(
            self.__class__.EVENT_WRITE, stream_id=stream_id, buffer=buffer
        )


class CloseStreamPacket(StreamForwardPacket):
    """Close Stream Packet"""

    def __init__(self, stream_id):
        super(CloseStreamPacket, self).__init__(
            self.__class__.EVENT_CLOSE, stream_id=stream_id
        )


class ICMPTunnelStreamManager(object):
    """ICMP Tunnel Stream Manager"""

    def __init__(self, session_stream, server_side=True):
        self._session_stream = session_stream
        self._server_side = server_side
        self._streams = {}
        self._event = asyncio.Event()
        self._running = True

    @property
    def session_stream(self):
        return self._session_stream

    def get_stream(self, stream_id):
        return self._streams.get(stream_id)

    async def wait_for_stream(self, address, timeout=10):
        time0 = time.time()
        key = "%s:%d" % address
        while time.time() - time0 < timeout:
            if not self._running:
                utils.logger.warning(
                    "[%s] Session stream %s:%d => %s:%d closed"
                    % (
                        self.__class__.__name__,
                        self._session_stream.client_address[0] or "0.0.0.0",
                        self._session_stream.client_address[1],
                        self._session_stream.server_address[0],
                        self._session_stream.server_address[1],
                    )
                )
                return -1
            if key in self._streams:
                return self._streams.pop(key)
            await self._event.wait()
            self._event.clear()
        utils.logger.warning(
            "[%s] Wait for stream %s complete timeout" % (self.__class__.__name__, key)
        )
        return -1

    def add_stream(self, stream_id):
        assert stream_id not in self._streams
        utils.logger.debug("[%s] Add stream %d" % (self.__class__.__name__, stream_id))
        self._streams[stream_id] = ICMPTunnelStream(stream_id, self._session_stream)
        return self._streams[stream_id]

    async def create_stream(self, target_address):
        raise NotImplementedError()

    async def handle_session_stream(self):
        buffer = b""
        while self._running:
            try:
                buffer += await self._session_stream.read()
            except utils.TunnelClosedError:
                self._running = False
                self._event.set()
                break
            if len(buffer) < 5:
                continue
            buffer_len = struct.unpack("!I", buffer[:4])[0]
            if len(buffer) < buffer_len + 4:
                continue
            buff = buffer[: buffer_len + 4]
            buffer = buffer[buffer_len + 4 :]
            stream_packet = StreamForwardPacket.unserialize_from(buff)
            source_address = (
                self._session_stream.client_address
                if self._server_side
                else self._session_stream.server_address
            )
            utils.logger.debug(
                "[%s] Received %s message from %s:%d, size=%d"
                % (
                    self.__class__.__name__,
                    stream_packet.event.upper(),
                    source_address[0],
                    source_address[1],
                    len(buff),
                )
            )
            if stream_packet.event == StreamForwardPacket.EVENT_CREATE:
                target_address = tuple(stream_packet.target_address)
                if self._server_side:
                    utils.safe_ensure_future(self.create_stream(target_address))
                else:
                    stream_id = stream_packet.result
                    self._streams["%s:%s" % target_address] = stream_id
                    utils.logger.info(
                        "[%s] Connect to %s:%d %s, stream_id is %d"
                        % (
                            self.__class__.__name__,
                            target_address[0],
                            target_address[1],
                            "success" if stream_id > 0 else "failed",
                            stream_id,
                        )
                    )
                    self.add_stream(stream_id)
                    self._event.set()
                continue
            stream = self._streams.get(stream_packet.stream_id)
            if not stream:
                utils.logger.warning(
                    "[%s] Unexpected %s message from stream %d"
                    % (
                        self.__class__.__name__,
                        stream_packet.event,
                        stream_packet.stream_id,
                    )
                )
                continue
            if stream_packet.event == StreamForwardPacket.EVENT_WRITE:
                stream.on_recv_data(stream_packet.buffer)
            elif stream_packet.event == StreamForwardPacket.EVENT_CLOSE:
                stream.on_close()
            else:
                raise NotImplementedError(stream_packet.event)


class ICMPTunnelStream(object):
    def __init__(self, stream_id, session_stream):
        self._stream_id = stream_id
        self._session_stream = session_stream
        self._buffer = b""
        self._running = True
        self._event = asyncio.Event()

    @property
    def ident(self):
        return self._stream_id

    @property
    def client_address(self):
        return self._session_stream.client_address

    @property
    def server_address(self):
        return self._session_stream.server_address

    def on_recv_data(self, buffer):
        self._buffer += buffer
        self._event.set()

    def on_close(self):
        self._running = False

    async def read(self):
        while self._running:
            if self._buffer:
                buffer = self._buffer
                self._buffer = b""
                return buffer
            await self._event.wait()
            self._event.clear()
        raise utils.TunnelClosedError()

    async def write(self, buffer):
        stream_packet = WriteStreamPacket(self._stream_id, buffer)
        await self._session_stream.write(stream_packet.serialize())

    def close(self):
        stream_packet = CloseStreamPacket(self._stream_id)
        utils.safe_ensure_future(self._session_stream.write(stream_packet.serialize()))


class ICMPTunnel(tunnel.Tunnel):
    """ICMP Tunnel"""

    icmp_socks = {}
    stream_managers = {}

    def __init__(self, tunnel, url=None, address=None):
        super(ICMPTunnel, self).__init__(tunnel, url, address)
        self._sock = None
        # url params are raw strings, so convert before use
        # `timeout` is the connect timeout passed to ICMPTransportClientSocket.connect.
        # `ping_timeout` (when present) is negotiated with the server as the
        # per-session PONG delay; when omitted the server keeps its own default.
        timeout_raw = self._url.params.get("timeout")
        if timeout_raw is not None:
            self._timeout = float(timeout_raw)
        else:
            self._timeout = 10
        ping_timeout_raw = self._url.params.get("ping_timeout")
        self._ping_timeout = float(ping_timeout_raw) if ping_timeout_raw is not None else None
        self._max_data_size = int(self._url.params.get("mss", 0)) or None
        self._auth = None
        if self._url.auth and ":" in self._url.auth:
            self._auth = self._url.auth.split(":", 1)
        self._stream_id = -1
        self._stream = None

    @classmethod
    def has_cache(cls, url):
        key = "%s:%d" % url.address
        if key in cls.icmp_socks:
            if cls.icmp_socks[key].status in (
                ICMPTransportSocket.STATUS_NOT_CONNECT,
                ICMPTransportSocket.STATUS_DISCONNECT,
            ):
                cls.icmp_socks.pop(key)
                return False
            return True
        return False

    async def connect(self):
        key = "%s:%d" % self._url.address
        if key in self.__class__.icmp_socks:
            self._sock = self.__class__.icmp_socks[key]
        else:
            self._sock = ICMPTransportClientSocket(self._max_data_size, self._auth)
        # Propagate the client-negotiated PONG delay (may be None -> server default)
        self._sock._ping_timeout = self._ping_timeout
        if self._sock.status in (
            ICMPTransportSocket.STATUS_NOT_CONNECT,
            ICMPTransportSocket.STATUS_DISCONNECT,
        ):
            if not await self._sock.connect(self._url.address, self._timeout):
                return False
            self.__class__.icmp_socks[key] = self._sock
            stream_manager = ICMPTunnelStreamManager(self._sock.session_stream, False)
            utils.safe_ensure_future(stream_manager.handle_session_stream())
            self.__class__.stream_managers[key] = stream_manager
        elif self._sock.status == ICMPTransportSocket.STATUS_CONNECTING:
            raise NotImplementedError(self._sock.status)
        stream_packet = CreateStreamPacket((self._addr, self._port))
        await self._sock.session_stream.write(stream_packet.serialize())
        if key not in self.__class__.stream_managers:
            utils.logger.error(
                "[%s] Stream manager of %s not found" % (self.__class__.__name__, key)
            )
            return False
        stream_manager = self.__class__.stream_managers[key]
        stream_id = await stream_manager.wait_for_stream((self._addr, self._port))
        if stream_id > 0:
            self._stream = stream_manager.get_stream(stream_id)
            return True
        else:
            return False

    async def write(self, buffer):
        if not isinstance(buffer, bytes):
            buffer = buffer.encode()
        return await self._stream.write(buffer)

    async def read(self):
        return await self._stream.read()

    def close(self):
        if self._stream:
            self._stream.close()
            self._stream = None


class ICMPTunnelServer(server.TunnelServer):
    """ICMP Tunnel Server"""

    def post_init(self):
        super(ICMPTunnelServer, self).post_init()
        this = self
        # `mss` caps the payload of a single ICMP packet, for networks whose
        # path MTU is smaller than the 1440 default (VPN, PPPoE, mobile).
        max_data_size = int(self._listen_url.params.get("mss", 0)) or None
        auth = None
        if self._listen_url.auth and ":" in self._listen_url.auth:
            auth = self._listen_url.auth.split(":", 1)

        # Delay before replying to a client PING with a PONG. Configurable via the
        # `ping_timeout` listen-URL parameter (formerly `timeout` / `pong_delay`), e.g.
        # `icmp://0.0.0.0?ping_timeout=0.5`.
        ping_timeout = float(
            self._listen_url.params.get(
                "ping_timeout",
                self._listen_url.params.get(
                    "timeout",
                    self._listen_url.params.get("pong_delay", PING_TIMEOUT_DEFAULT),
                ),
            )
        )

        class ICMPTunnelServerStreamManager(ICMPTunnelStreamManager):
            async def create_stream(self, target_address):
                stream_packet = CreateStreamPacket(target_address)
                with server.TunnelConnection(
                    self._session_stream.client_address,
                    target_address,
                    this.final_tunnel and this.final_tunnel.address,
                ) as tun_conn:
                    with this.create_tunnel_chain() as tunnel_chain:
                        try:
                            await tunnel_chain.create_tunnel(target_address)
                        except utils.TunnelError as e:
                            if not isinstance(e, utils.TunnelBlockedError):
                                utils.logger.warn(
                                    "[%s] Connect %s:%d failed: %s"
                                    % (
                                        self.__class__.__name__,
                                        target_address[0],
                                        target_address[1],
                                        e,
                                    )
                                )
                            stream_packet.result = -1
                            await self._session_stream.write(stream_packet.serialize())
                        else:
                            stream_id = random.randint(1, pow(2, 32) - 1)
                            stream_packet.result = stream_id
                            await self._session_stream.write(stream_packet.serialize())
                            stream = self.add_stream(stream_id)
                            tasks = [
                                this.forward_data_to_upstream(
                                    tun_conn, stream, tunnel_chain.tail
                                ),
                                this.forward_data_to_downstream(
                                    tun_conn, stream, tunnel_chain.tail
                                ),
                            ]
                            await utils.AsyncTaskManager().wait_for_tasks(tasks)

        class ICMPForwardStreamHandler(ICMPTunnelStreamHandler):
            async def handle_session_stream(self, session_stream):
                stream_manager = ICMPTunnelServerStreamManager(session_stream)
                await stream_manager.handle_session_stream()

        self._server = ICMPTransportServerSocket(
            ICMPForwardStreamHandler, max_data_size, auth, ping_timeout=ping_timeout
        )

    def start(self):
        if sys.platform == "linux":
            # On Linux the kernel answers ICMP echo requests destined to the host
            # itself, which would collide with the tunnel, so we silence it.
            disable_ping_file = "/proc/sys/net/ipv4/icmp_echo_ignore_all"
            try:
                with open(disable_ping_file) as fp:
                    text = fp.read()
                if text.strip() == "0":
                    utils.logger.info("Disable icmp echo replay on Linux")
                    with open(disable_ping_file, "w") as fp:
                        fp.write("1")
            except OSError:
                # procfs knob missing (container, etc.) -- nothing we can do.
                pass
        # On Windows, AsyncICMPSocket uses SIO_RCVALL and the per-packet
        # direction bit, so the kernel's own echo replies are filtered out at the
        # receiver; there is nothing to disable here.
        utils.safe_ensure_future(self._server.listen(self._listen_url.address))
        utils.logger.info(
            "[%s] ICMP tunnel server is listening on %s:%d"
            % (self.__class__.__name__, self._listen_url.host, self._listen_url.port)
        )


registry.tunnel_registry.register("icmp", ICMPTunnel)
registry.server_registry.register("icmp", ICMPTunnelServer)
