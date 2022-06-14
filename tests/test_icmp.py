# -*- coding: utf-8 -*-

import asyncio
import os
import sys

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


@root_required
def teardown_module(module):
    if sys.platform != "linux":
        print("Ignore module teardown")
        return

    with open(disable_ping_file, "w") as fp:
        fp.write("0")
    print(exec_command("ip link delete veth1"))
    print(exec_command("ip netns delete netns1"))


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


@root_required
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
