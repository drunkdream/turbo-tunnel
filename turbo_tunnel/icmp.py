# -*- coding: utf-8 -*-

"""ICMP Tunnel
"""

import asyncio
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
        addr, _ = address
        if addr not in self._buffers:
            self._buffers[addr] = []
        self._buffers[addr].append(data[20:])  # Ignore 20 bytes ip header
        self._event.set()

    def error_received(self, exc):
        print(exc)


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
                self._sock.bind(("0.0.0.0", 0))
        except PermissionError:
            raise RuntimeError("Raw socket need root permission")
        else:
            self._sock.setblocking(False)
        self._transport = None
        self._protocol = ICMPProtocol()
        self._seq = 1

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
        ident = ident or random.randint(0, 65535)
        if not seq:
            seq = self._seq
            self._seq += 1
        icmp_packet = ICMPPacket(type, code, ident, seq, buffer)
        self._transport.sendto(icmp_packet.serialize(), address)
        utils.logger.verbose(
            "[%s] Send %d bytes icmp data to %s, ident=%d, seq=%d"
            % (
                self.__class__.__name__,
                len(buffer.rstrip(b"\x00")),
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
    | ----------- Event ----------- |
    | ------------- 4 ------------- |
    | -- Local ID - | - Remote ID - |
    | ----- 2 ----- | ----- 2 ----- |
    |            Padding            |
    """

    MAGIC_FLAG = b"TTUN"
    EVENT_CONNECT = b"CNCT"
    EVENT_OK = b"OKAY"
    EVENT_FAIL = b"FAIL"
    EVENT_WRITE = b"WRTE"
    EVENT_PING = b"PING"
    EVENT_PONG = b"PONG"
    EVENT_CLOSE = b"CLSE"
    EVENT_RESET = b"REST"
    MAX_DATA_SIZE = 1440

    def __init__(self, seq, ack, event, client_port, server_port, padding):
        self._seq = seq
        self._ack = ack
        self._event = event
        self._client_port = client_port
        self._server_port = server_port
        self._padding = padding

    @property
    def seq_num(self):
        return self._seq

    @property
    def ack_num(self):
        return self._ack

    @property
    def event(self):
        return self._event

    @property
    def client_port(self):
        return self._client_port

    @property
    def server_port(self):
        return self._server_port

    @property
    def padding(self):
        return self._padding

    def serialize(self):
        buffer = self.__class__.MAGIC_FLAG
        buffer += struct.pack("!HHHH", 20 + len(self._padding), 0, self._seq, self._ack)
        buffer += self._event
        buffer += struct.pack("!HH", self._client_port, self._server_port)
        buffer += self._padding
        checksum = utils.checksum(buffer)
        buffer = buffer[:6] + struct.pack("!H", checksum) + buffer[8:]
        if len(self._padding) < self.__class__.MAX_DATA_SIZE:
            buffer += b"\x00" * (self.__class__.MAX_DATA_SIZE - len(self._padding))
        return buffer

    @staticmethod
    def unserialize_from(buffer):
        if len(buffer) < 20 or not buffer.startswith(ICMPTransportPacket.MAGIC_FLAG):
            raise utils.TunnelPacketError("Invalid ICMP transport packet: %r" % buffer)
        total_length, checksum, seq, ack = struct.unpack("!HHHH", buffer[4:12])
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
        event = buffer[12:16]
        client_port, server_port = struct.unpack("!HH", buffer[16:20])
        return ICMPTransportPacket(
            seq, ack, event, client_port, server_port, buffer[20:]
        )


class ICMPSessionStream(object):
    """ICMP Transport Session Stream"""

    DELAY_ACK_TIME = 0.1

    def __init__(self, sock, client_address, server_address, server_side=True):
        self._sock = sock
        self._client_address = client_address
        self._server_address = server_address
        self._server_side = server_side
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
        await self._sock.send(self, ICMPTransportPacket.EVENT_CLOSE)
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
                if len(trans_packet.padding) < ICMPTransportPacket.MAX_DATA_SIZE
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

    def add_response_slot(self, ident, seq):
        self._response_slots.append((ident, seq, time.time()))

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
            ICMPTransportPacket.EVENT_PONG
            if self._server_side
            else ICMPTransportPacket.EVENT_PING
        )

        if self._server_side:
            if (
                time.time() - self._last_pong_time < delay_time
                or not self.has_response_slot()
            ):
                utils.logger.verbose(
                    "[%s] Ignore send pong packet, last pong time is %.2fs ago"
                    % (self.__class__.__name__, time.time() - self._last_pong_time)
                )
                return
            await self._sock.send(self, event, msg_seq=0, wait_for_ack=False)
            self._last_pong_time = time.time()
        else:
            await self._sock.send(event, msg_seq=0, wait_for_ack=False)


class SessionStreamManager(object):
    def __init__(self):
        self._session_streams = []

    def get_session_stream(self, client_address, server_address):
        for stream in self._session_streams:
            if (
                stream.client_address == client_address
                and stream.server_address[1] == server_address[1]
            ):
                return stream
        return None

    def on_new_session(self, sock, client_address, server_address):
        session_stream = ICMPSessionStream(sock, client_address, server_address)
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


class ICMPTransportSocket(object):

    MAX_WINDOW_SIZE = 5
    ACK_TIMEOUT = 2
    SEND_TIMEOUT = 60

    PING_INTERVAL = 5
    PING_TIMEOUT = 30

    STATUS_NOT_CONNECT = 0
    STATUS_CONNECTING = 1
    STATUS_ESTABLISHED = 2
    STATUS_DISCONNECT = 3

    def __init__(self, server_side=False):
        self._sock = AsyncICMPSocket()
        self._server_side = server_side
        self._sending_buffers = []
        self._unack_packets = 0
        self._last_acks = {}
        self._last_send_times = {}
        self._running = True
        utils.safe_ensure_future(self.send_packets_task())

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
            if message["count"] == 0:
                utils.logger.info(
                    "[%s] Ignore send messages: %s"
                    % (
                        self.__class__.__name__,
                        ", ".join([str(it["msg_seq"]) for it in delete_list]),
                    )
                )
            for it in delete_list:
                self._sending_buffers.remove(it)

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
        while self._running:
            if not self._sending_buffers:
                await asyncio.sleep(0.005)

            for message in self._sending_buffers:
                if (
                    message["priority"] == 0
                    and self._unack_packets >= self.__class__.MAX_WINDOW_SIZE
                ):
                    continue
                if time.time() - message["timestamp"] < self.__class__.ACK_TIMEOUT:
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
                    message["count"] += 1
                    self._unack_packets += 1
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
                event.decode(),
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
            msg_seq, msg_ack, event, client_port, server_port, buffer
        )
        buffer = trans_packet.serialize()
        icmp_type = (
            EnumICMPType.ECHO if not self._server_side else EnumICMPType.ECHO_REPLY
        )
        icmp_code = EnumICMPEchoCode.NO_CODE
        address = (address, client_port if self._server_side else server_port)
        priority = 0
        if event == ICMPTransportPacket.EVENT_PING:
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

    def __init__(self):
        super(ICMPTransportClientSocket, self).__init__(server_side=False)
        self._local_address = None
        self._remote_address = None
        self._session_stream = None
        self._next_seq = 1
        self._status = ICMPTransportSocket.STATUS_NOT_CONNECT
        self._last_pong_time = 0
        self._current_pings = {}

    @property
    def session_stream(self):
        return self._session_stream

    @property
    def status(self):
        return self._status

    async def send(self, event, buffer=b"", msg_seq=None, wait_for_ack=True):
        assert self._session_stream is not None
        assert self._status != ICMPTransportSocket.STATUS_DISCONNECT
        assert msg_seq is None or isinstance(msg_seq, int)
        if msg_seq is None:
            msg_seq = self._session_stream.next_seq
        ack_num = self._session_stream.next_ack
        self._session_stream.on_send_ack(ack_num)
        icmp_ident = None
        if event == ICMPTransportPacket.EVENT_PING:
            icmp_ident = random.randint(0, 65535)
            self._current_pings[icmp_ident] = time.time()
        return await super(ICMPTransportClientSocket, self).send(
            self._remote_address[0],
            msg_seq,
            ack_num,
            event,
            self._local_address[1],
            self._remote_address[1],
            buffer,
            icmp_ident=icmp_ident,
            wait_for_ack=wait_for_ack,
        )

    async def send_message(self, buffer):
        offset = 0
        if len(buffer) > ICMPTransportPacket.MAX_DATA_SIZE:
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
                ICMPTransportPacket.EVENT_WRITE,
                buffer[offset : offset + ICMPTransportPacket.MAX_DATA_SIZE],
            )
            offset += ICMPTransportPacket.MAX_DATA_SIZE

    async def handling_message_task(self, address):
        icmp_type = EnumICMPType.ECHO_REPLY
        icmp_code = EnumICMPEchoCode.NO_CODE
        while self._status != ICMPTransportSocket.STATUS_DISCONNECT:
            addr, icmp_packet = await self._sock.recvfrom(
                address=address, type=icmp_type, code=icmp_code
            )
            buffer = icmp_packet.data
            if not buffer.startswith(ICMPTransportPacket.MAGIC_FLAG):
                utils.logger.info(
                    "[%s] Ignore unexpected pong packet: length=%d"
                    % (
                        self.__class__.__name__,
                        len(buffer),
                    )
                )
                continue

            try:
                trans_packet = ICMPTransportPacket.unserialize_from(buffer)
            except utils.TunnelPacketError as ex:
                utils.logger.warning(
                    "[%s] Unserialize icmp tunel packet failed: %s"
                    % (self.__class__.__name__, ex)
                )
                continue

            if trans_packet.server_port != self._remote_address[1]:
                utils.logger.warning(
                    "[%s] ICMP server port mismatch: %d vs %d"
                    % (
                        self.__class__.__name__,
                        self._remote_address[1],
                        trans_packet.server_port,
                    )
                )
                continue

            message = "[%s] Received %s message from %s:%d, seq=%d, ack=%d, length=%d, icmp_ident=%d, icmp_seq=%d" % (
                self.__class__.__name__,
                trans_packet.event.decode(),
                addr,
                trans_packet.server_port,
                trans_packet.seq_num,
                trans_packet.ack_num,
                len(trans_packet.padding),
                icmp_packet.ident,
                icmp_packet.sequence,
            )
            if icmp_packet.ident in self._current_pings:
                request_time = self._current_pings.pop(icmp_packet.ident)
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
                        trans_packet.event.decode(),
                        self._status,
                    )
                )
                continue
            elif self._status == ICMPTransportSocket.STATUS_CONNECTING:
                if trans_packet.event == ICMPTransportPacket.EVENT_OK:
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
                elif trans_packet.event == ICMPTransportPacket.EVENT_FAIL:
                    self._status = ICMPTransportSocket.STATUS_DISCONNECT
                else:
                    utils.logger.error(
                        "[%s] Unexpected packet event=%s when connecting"
                        % (self.__class__.__name__, trans_packet.event.decode())
                    )
                    continue
            elif self._status == ICMPTransportSocket.STATUS_ESTABLISHED:
                if trans_packet.event == ICMPTransportPacket.EVENT_RESET:
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
                elif trans_packet.event == ICMPTransportPacket.EVENT_WRITE:
                    self._session_stream.on_data_received(
                        trans_packet.seq_num, trans_packet.padding
                    )
                elif trans_packet.event == ICMPTransportPacket.EVENT_CLOSE:
                    self._status = ICMPTransportSocket.STATUS_DISCONNECT
                elif trans_packet.event == ICMPTransportPacket.EVENT_PONG:
                    self._last_pong_time = time.time()
                else:
                    utils.logger.warning(
                        "[%s] Unexpected packet event=%s when connection established"
                        % (self.__class__.__name__, trans_packet.event.decode())
                    )
                    continue
        self._running = False
        utils.logger.info("[%s] Handling message task exit" % self.__class__.__name__)

    async def connect(self, address, timeout=10):
        assert self._remote_address is None
        await self._sock.start()
        self._remote_address = address
        self._local_address = (None, random.randint(0, 65535))  # FIXME
        self._session_stream = ICMPSessionStream(
            self, self._local_address, self._remote_address, server_side=False
        )
        utils.safe_ensure_future(self.handling_message_task(self._remote_address[0]))
        self._status = ICMPTransportSocket.STATUS_CONNECTING
        last_send_time = time0 = time.time()
        await self.send(ICMPTransportPacket.EVENT_CONNECT)
        while time.time() - time0 < timeout:
            if self._status == ICMPTransportSocket.STATUS_ESTABLISHED:
                utils.safe_ensure_future(self.ping_task())
                return True
            elif self._status == ICMPTransportSocket.STATUS_DISCONNECT:
                return False
            if time.time() - last_send_time >= 1:
                await self.send(ICMPTransportPacket.EVENT_CONNECT)
                last_send_time = time.time()
            await asyncio.sleep(0.005)
        else:
            return False

    async def ping_task(self):
        min_ping_concurrency = 1
        ping_concurrency = min_ping_concurrency
        while self._status == ICMPTransportSocket.STATUS_ESTABLISHED:
            idle_time = time.time() - self._session_stream.last_alive_time
            if idle_time >= self.__class__.PING_TIMEOUT * 3:
                # connection timeout
                await self.send(ICMPTransportPacket.EVENT_CLOSE, wait_for_ack=False)
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
                for _ in range(ping_concurrency - len(current_pings)):
                    await self.send(
                        ICMPTransportPacket.EVENT_PING, msg_seq=0, wait_for_ack=False
                    )

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

    def __init__(self, stream_handler_cls):
        super(ICMPTransportServerSocket, self).__init__(True)
        self._stream_handler_cls = stream_handler_cls
        self._listen_address = None
        self._sessmgr = SessionStreamManager()

    async def send(
        self, session_stream, event, buffer=b"", msg_seq=None, wait_for_ack=True
    ):
        if msg_seq is None:
            msg_seq = session_stream.next_seq
        ack_num = session_stream.next_ack
        session_stream.on_send_ack(ack_num)
        ident, seq = await session_stream.get_response_slot()
        return await super(ICMPTransportServerSocket, self).send(
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

    async def send_message(self, session_stream, buffer):
        offset = 0
        if len(buffer) > ICMPTransportPacket.MAX_DATA_SIZE:
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
                ICMPTransportPacket.EVENT_WRITE,
                buffer[offset : offset + ICMPTransportPacket.MAX_DATA_SIZE],
            )
            offset += ICMPTransportPacket.MAX_DATA_SIZE

    async def send_reset(self, client_address, server_address, ident, seq):
        return await super(ICMPTransportServerSocket, self).send(
            client_address[0],
            0,
            0,
            ICMPTransportPacket.EVENT_RESET,
            client_address[1],
            server_address[1],
            b"",
            icmp_ident=ident,
            icmp_seq=seq,
            wait_for_ack=False,
        )

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
                    trans_packet.event.decode(),
                    addr,
                    trans_packet.client_port,
                    trans_packet.seq_num,
                    trans_packet.ack_num,
                    len(trans_packet.padding),
                    icmp_packet.ident,
                    icmp_packet.sequence,
                )
            )

            if trans_packet.event == ICMPTransportPacket.EVENT_CONNECT:
                session_stream, result = self._sessmgr.on_new_session(
                    self, (addr, trans_packet.client_port), self._listen_address
                )
                session_stream.on_message_received(trans_packet)
                session_stream.on_data_received(trans_packet.seq_num, b"")
                session_stream.add_response_slot(
                    icmp_packet.ident, icmp_packet.sequence
                )
                if result:

                    async def _handle_new_stream():
                        await self.send(
                            session_stream,
                            ICMPTransportPacket.EVENT_OK,
                        )
                        await self._stream_handler_cls().handle_session_stream(
                            session_stream
                        )

                    utils.safe_ensure_future(_handle_new_stream())
                    session_stream.status = ICMPTransportSocket.STATUS_ESTABLISHED
                else:
                    utils.safe_ensure_future(
                        self.send(
                            session_stream,
                            ICMPTransportPacket.EVENT_FAIL,
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
                session_stream.on_message_received(trans_packet)
                session_stream.add_response_slot(
                    icmp_packet.ident, icmp_packet.sequence
                )
                if trans_packet.seq_num and trans_packet.padding:
                    session_stream.on_data_received(
                        trans_packet.seq_num, trans_packet.padding
                    )

                if trans_packet.event == ICMPTransportPacket.EVENT_PING:
                    utils.safe_ensure_future(
                        session_stream.delay_ack(
                            0,
                            self.__class__.PING_INTERVAL,
                        )
                    )
                elif trans_packet.event == ICMPTransportPacket.EVENT_WRITE:
                    pass
                else:
                    utils.logger.warning(
                        "[%s] Unexpected icmp transport packet, event=%s"
                        % (self.__class__.__name__, trans_packet.event.decode())
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
        self._timeout = self._url.params.get("timeout", 10)
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
            self._sock = ICMPTransportClientSocket()
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
                "[%s] Stream manager of %s not found" % (self.__class__._name__, key)
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

        self._server = ICMPTransportServerSocket(ICMPForwardStreamHandler)

    def start(self):
        if sys.platform != "linux":
            raise NotImplementedError("System %s not supported" % sys.platform)
        disable_ping_file = "/proc/sys/net/ipv4/icmp_echo_ignore_all"
        with open(disable_ping_file) as fp:
            text = fp.read()
        if text.strip() == "0":
            utils.logger.info("Disable icmp echo replay")
            with open(disable_ping_file, "w") as fp:
                fp.write("1")
        utils.safe_ensure_future(
            self._server.listen((self._listen_url.host, self._listen_url.port))
        )
        utils.logger.info(
            "[%s] ICMP tunnel server is listening on %s:%d"
            % (self.__class__.__name__, self._listen_url.host, self._listen_url.port)
        )


registry.tunnel_registry.register("icmp", ICMPTunnel)
registry.server_registry.register("icmp", ICMPTunnelServer)
