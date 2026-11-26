# -*- coding: utf-8 -*-

import asyncio
import os
import sys
import time

import pytest

from turbo_tunnel import icmp
from turbo_tunnel import utils

disable_ping_file = "/proc/sys/net/ipv4/icmp_echo_ignore_all"


def exec_command(cmdline, sync=True, raise_for_error=True):
    proc = os.popen(cmdline)
    if not sync:
        return proc
    stdout = proc.read()
    exit_code = proc.close()
    if exit_code:
        err_msg = "Exec command `%s` failed: %d" % (cmdline, exit_code)
        if raise_for_error:
            raise RuntimeError(err_msg)
        else:
            print(err_msg, file=sys.stderr)
    return stdout


# Check if we should skip root-required tests
skip_root_tests = sys.platform != "linux" or os.getuid() != 0
skip_reason = "Requires Linux and root privileges (sudo)"


@pytest.mark.skipif(skip_root_tests, reason=skip_reason)
def setup_module(module):
    if skip_root_tests:
        return
    with open(disable_ping_file, "w") as fp:
        fp.write("1")

    print(exec_command("ip link delete veth1", raise_for_error=False))
    print(exec_command("ip netns delete netns1", raise_for_error=False))
    print(exec_command("ip netns add netns1"))
    print(exec_command("ip netns exec netns1 ip link set lo up"))
    print(exec_command("ip netns exec netns1 sh -c 'echo 1 > %s'" % disable_ping_file))
    print(exec_command("ip link add veth1 type veth peer name veth2"))
    print(exec_command("ip link set veth2 netns netns1"))
    print(exec_command("ip netns exec netns1 ip link set veth2 up"))
    print(exec_command("ip netns exec netns1 ip addr add 192.168.100.2/24 dev veth2"))
    print(exec_command("ip link set veth1 up"))
    print(exec_command("ip addr add 192.168.100.1/24 dev veth1"))


@pytest.mark.skipif(skip_root_tests, reason=skip_reason)
def teardown_module(module):
    if skip_root_tests:
        return
    with open(disable_ping_file, "w") as fp:
        fp.write("0")
    print(exec_command("ip link delete veth1"))
    print(exec_command("ip netns delete netns1"))


@pytest.mark.skipif(skip_root_tests, reason=skip_reason)
async def test_icmp_socket():
    address = "127.0.0.1"
    server_sock = icmp.AsyncICMPSocket()
    await server_sock.start()
    client_sock = icmp.AsyncICMPSocket()
    await client_sock.start()
    buffer = b"12345"
    client_sock.sendto(buffer, address)
    _, icmp_packet = await server_sock.recvfrom(address)
    server_sock.sendto(icmp_packet.data * 2, address)
    _, icmp_packet = await client_sock.recvfrom(address)
    assert icmp_packet.data == buffer * 2


@pytest.mark.skipif(skip_root_tests, reason=skip_reason)
async def test_icmp_tunnel():
    event = asyncio.Event()

    async def async_run_process(cmdline):
        import signal

        proc = await asyncio.create_subprocess_shell(cmdline, preexec_fn=os.setsid)
        await event.wait()
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        print("Process `%s` is killed" % cmdline)

    asyncio.ensure_future(
        async_run_process(
            "ip netns exec netns1 %s -m http.server 8888" % sys.executable
        )
    )
    asyncio.ensure_future(
        async_run_process(
            "ip netns exec netns1 %s -m turbo_tunnel -l icmp://192.168.100.2:8000/ --log-level verbose"
            % sys.executable
        )
    )
    await asyncio.sleep(2)

    tunn = icmp.ICMPTunnel(
        None, utils.Url("icmp://192.168.100.2:8000/"), ("192.168.100.2", 8888)
    )
    assert await tunn.connect()
    await tunn.write(b"GET / HTTP/1.1\r\n\r\n")
    response = (await tunn.read()).decode()
    assert response.startswith("HTTP/1.0 200 OK\r\n")
    event.set()


class FakeSessionSocket(object):
    """Minimal socket stub for ICMPSessionStream unit tests."""

    max_data_size = 1200

    def __init__(self):
        self.acks = []

    def on_ack_received(self, address, ack):
        self.acks.append(ack)


def make_session_stream(server_side=False):
    return icmp.ICMPSessionStream(
        FakeSessionSocket(), ("1.2.3.4", 1234), ("5.6.7.8", 8000),
        server_side=server_side,
    )


def test_next_ack_is_monotonic_across_gap_and_retransmit():
    """Cumulative ACK must never regress when an old fragment is redelivered.

    A regression made the peer release fragments we never received and stop
    hole-filling the real gap, stalling large transfers on lossy links.
    """
    stream = make_session_stream()
    for seq in list(range(1, 11)) + list(range(12, 21)):
        stream.on_data_received(seq, b"x")
    assert stream.next_ack == 10

    # A retransmitted old fragment must not pull the ACK back.
    stream.on_data_received(5, b"x")
    assert stream.next_ack == 10

    # Filling the gap jumps the ACK to the highest contiguous fragment.
    stream.on_data_received(11, b"x")
    assert stream.next_ack == 20


async def test_session_stream_read_keeps_event_and_buffer_in_sync():
    """A stale wakeup must never produce an empty read or swallow new data."""
    stream = make_session_stream()
    stream.on_data_received(1, b"A")
    assert stream._recv_event.is_set()

    assert await stream.read() == b"A"
    # The event is cleared after a successful read.
    assert not stream._recv_event.is_set()

    async def deliver_later():
        await asyncio.sleep(0.01)
        stream.on_data_received(2, b"B")

    task = asyncio.ensure_future(deliver_later())
    got = await asyncio.wait_for(stream.read(), 1)
    await task
    assert got == b"B"


def test_sack_list_reports_out_of_order_fragments():
    stream = make_session_stream()
    stream.on_data_received(1, b"A")
    stream.on_data_received(2, b"B")
    stream.on_data_received(4, b"D")
    stream.on_data_received(5, b"E")
    assert stream.sack_list == [4, 5]
    assert stream.next_ack == 2


def test_release_downlink_sack_only_frees_reported_fragments():
    stream = make_session_stream(server_side=True)
    now = time.time()
    for seq in (1, 2, 3):
        stream._downlink_unacked[seq] = (b"x", icmp.ICMPTransportPacket.OP_DATA, now, 0)
    stream.release_downlink_sack([3])
    assert 1 in stream._downlink_unacked
    assert 2 in stream._downlink_unacked
    assert 3 not in stream._downlink_unacked


def test_sack_parsed_from_ping_payload():
    stream = make_session_stream(server_side=True)
    now = time.time()
    stream._downlink_unacked[5] = (b"E", icmp.ICMPTransportPacket.OP_DATA, now, 0)
    payload = icmp._pack_msgpack_payload({"sack": [5]})
    packet = icmp.ICMPTransportPacket(
        0, 1, icmp.ICMPTransportPacket.OP_PING, 1, 2, payload, direction=1
    )
    stream.on_message_received(packet)
    assert 5 not in stream._downlink_unacked


async def test_tunnel_stream_read_drains_buffer_before_closing():
    stream = icmp.ICMPTunnelStream(1, make_session_stream())
    stream.on_recv_data(b"A")
    stream.on_close()
    # Buffered data is delivered even after close; only then does read raise.
    assert await stream.read() == b"A"
    with pytest.raises(utils.TunnelClosedError):
        await stream.read()


def test_ping_concurrency_adjustment():
    adjust = icmp.ICMPTransportClientSocket.adjust_ping_concurrency

    # Fresh downlink data jumps straight to the full window.
    assert adjust(1, 0.05) == icmp.ICMPTransportClientSocket.MAX_PING_CONCURRENCY
    # Normal DATA RTT (1-2s) must not collapse the window.
    assert adjust(20, 1.0) == 20
    assert adjust(20, 1.9) == 20
    # Longer idle decays proportionally, never below the minimum.
    assert adjust(20, 3.0) == 15
    assert adjust(15, 3.0) == 11
    assert adjust(1, 3.0) == 1


async def test_session_manager_drains_multiple_frames_from_one_read():
    """Multiple stream frames in one session read must all be dispatched."""
    session = make_session_stream()
    manager = icmp.ICMPTunnelStreamManager(session, server_side=False)
    stream = manager.add_stream(7)
    frames = (
        icmp.WriteStreamPacket(7, b"A").serialize()
        + icmp.WriteStreamPacket(7, b"B").serialize()
        + icmp.WriteStreamPacket(7, b"C").serialize()
    )

    # Feed all frames at once; the manager must not block waiting for more
    # data while frame B and C are still buffered.
    session._recv_buffer += frames
    session._recv_event.set()
    task = asyncio.ensure_future(manager.handle_session_stream())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert stream._buffer == b"ABC"


class TestStreamHandler(icmp.ICMPTunnelStreamHandler):
    buffer = b""

    async def handle_session_stream(self, session_stream):
        for _ in range(5):
            self.__class__.buffer += await session_stream.read()


# async def test_transport_socket():
#     address = ("127.0.0.1", 12345)
#     server_sock = icmp.ICMPTransportServerSocket(TestStreamHandler)
#     utils.safe_ensure_future(server_sock.listen(address))
#     client_sock = icmp.ICMPTransportClientSocket()
#     await client_sock.connect(address)
