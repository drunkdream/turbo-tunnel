# -*- coding: utf-8 -*-

import os
import sys

from turbo_tunnel import icmp
from turbo_tunnel import utils

disable_ping_file = "/proc/sys/net/ipv4/icmp_echo_ignore_all"


def root_required(func):
    if sys.platform != "linux":
        print("Unsupported system %s" % sys.platform, file=sys.stderr)
        return
    if os.getuid() != 0:
        print(
            "Ignore run function %s when run as user %d" % (func.__name__, os.getuid()),
            file=sys.stderr,
        )
        return
    return func


@root_required
def setup_module(module):
    if sys.platform != "linux":
        print("Ignore module setup")
        return
    with open(disable_ping_file, "w") as fp:
        fp.write("1")


@root_required
def teardown_module(module):
    if sys.platform != "linux":
        print("Ignore module teardown")
        return
    with open(disable_ping_file, "w") as fp:
        fp.write("0")


@root_required
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
