# -*- coding: utf-8 -*-

import asyncio
import random
import os
import socket
import sys

import asyncssh
import pytest

from turbo_tunnel import ssh, tunnel, utils
from .util import DemoTCPServer, get_random_port


async def start_ssh_server(port, username, password=None, public_key=None):
    if not os.path.isfile("skey"):
        skey = asyncssh.generate_private_key("ssh-rsa")
        skey.write_private_key("skey")
        skey.write_public_key("skey.pub")
    ssh_server = ssh.MicroSSHServer(
        ("127.0.0.1", port), ["skey"], username, password, public_key
    )
    await ssh_server.start()
    return ssh_server


class TestSSHServer(object):
    @classmethod
    def setup_class(cls):
        cls.username = "root"
        cls.password = "password"

    async def ensure_start_server(self):
        self.port = get_random_port()
        if not hasattr(self, "_server_started") or not self._server_started:
            self.server = await start_ssh_server(
                self.port, self.username, self.password
            )

    def ensure_stop_server(self):
        if getattr(self, "server", False):
            self.server.close()
            self.server = None

    async def test_invalid_password(self):
        await self.ensure_start_server()
        options = {
            "known_hosts": None,
        }
        options = asyncssh.SSHClientConnectionOptions(**options)
        with pytest.raises(asyncssh.PermissionDenied):
            await asyncssh.connect(
                "127.0.0.1",
                self.port,
                username=self.username,
                password="invalid password",
                options=options,
            )
        self.ensure_stop_server()

    async def test_exec_command(self):
        if sys.platform == "win32":
            # Ignore on windows currently
            return
        await self.ensure_start_server()
        options = {
            "known_hosts": None,
        }
        options = asyncssh.SSHClientConnectionOptions(**options)
        async with asyncssh.connect(
            "127.0.0.1",
            self.port,
            username=self.username,
            password=self.password,
            options=options,
        ) as conn:
            message = "Hello ssh!"
            result = await conn.run('echo "%s"' % message)  # , check=True
            # assert result.stdout.strip() == message
        self.ensure_stop_server()

    async def test_tcp_forward(self):
        await self.ensure_start_server()
        server = DemoTCPServer()
        port = get_random_port()
        server.listen(port)
        options = {
            "known_hosts": None,
        }
        options = asyncssh.SSHClientConnectionOptions(**options)
        async with asyncssh.connect(
            "127.0.0.1",
            self.port,
            username=self.username,
            password=self.password,
            options=options,
        ) as conn:
            message = b"Hello ssh!"
            reader, writer = await conn.open_connection("127.0.0.1", port)
            writer.write(message + b"\n")
            buffer = await reader.read(4096)
            assert buffer.strip() == message
        self.ensure_stop_server()


class TestSSHTunnel(object):
    @classmethod
    def setup_class(cls):
        cls.username = "root"
        cls.public_key = "password"
        cls.port = get_random_port()
        id_rsa = asyncssh.generate_private_key("ssh-rsa")
        id_rsa.write_private_key("id_rsa")
        id_rsa.write_public_key("id_rsa.pub")

    @classmethod
    def teardown_class(cls):
        if hasattr(cls, "server"):
            cls.server.close()

    async def ensure_start_server(self):
        if not hasattr(self, "_server_started") or not self._server_started:
            listen_url = "ssh://%s@127.0.0.1:%d/?public_key=id_rsa.pub" % (
                self.username,
                self.port,
            )
            self.__class__.server = ssh.SSHTunnelServer(listen_url, ["tcp://"])
            self.__class__.server.start()
            await asyncio.sleep(1)
            self._server_started = True

    async def test_tcp_forward(self):
        await self.ensure_start_server()
        server1 = DemoTCPServer()
        port1 = get_random_port()
        server1.listen(port1)

        s = socket.socket()
        tunn = tunnel.TCPTunnel(s, address=("127.0.0.1", self.port))
        await tunn.connect()
        url = "ssh://%s@127.0.0.1:%d/?private_key=id_rsa" % (self.username, self.port)
        ssh_tunn = ssh.SSHTunnel(tunn, utils.Url(url), address=("127.0.0.1", port1))
        await ssh_tunn.connect()
        message = b"Hello ssh!"
        await ssh_tunn.write(message + b"\n")
        buffer = await ssh_tunn.read()
        assert buffer.strip() == message

    async def test_fork_tunnel(self):
        await self.ensure_start_server()
        server1 = DemoTCPServer()
        port1 = get_random_port()
        server1.listen(port1)

        s = socket.socket()
        tunn = tunnel.TCPTunnel(s, address=("127.0.0.1", self.port))
        await tunn.connect()
        url = "ssh://%s@127.0.0.1:%d/?private_key=id_rsa" % (self.username, self.port)
        ssh_tunn = ssh.SSHTunnel(tunn, utils.Url(url), address=("127.0.0.1", port1))
        fork_ssh_tunn = await ssh_tunn.fork()
        message = b"Hello ssh!"
        await fork_ssh_tunn.write(message + b"\n")
        buffer = await fork_ssh_tunn.read()
        assert buffer.strip() == message
