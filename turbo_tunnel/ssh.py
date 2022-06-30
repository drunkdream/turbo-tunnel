# -*- coding: utf-8 -*-
"""SSH Tunnel
"""

import asyncio
import ctypes
import os
import platform
import shlex
import socket
import struct
import sys
import time

import asyncssh

from . import VERSION
from . import registry
from . import server
from . import tunnel
from . import utils


class MicroSSHServer(asyncssh.SSHServer):
    """Micro SSH Server"""

    barrier = b"=" * 80
    welcome = b"""Welcome to turbo-tunnel micro ssh server\r\n"""

    def __init__(
        self, listen_address, server_host_keys, username, password=None, public_key=None
    ):
        self._listen_address = listen_address
        self._server_host_keys = server_host_keys
        self._username = username
        self._password = password
        self._public_key = public_key
        self._conn = None
        self._hostname = socket.gethostname()
        self._pty_enabled = sys.platform != "win32" or hasattr(
            ctypes.windll.kernel32, "CreatePseudoConsole"
        )
        self._line_editor = not self._pty_enabled

    def connection_made(self, conn):
        """Record connection object for later use"""
        self._conn = conn
        self._conn._version = ("TurboTunnel_%s" % VERSION).encode()

    def begin_auth(self, username):
        """Handle client authentication request"""
        return username == self._username

    def password_auth_supported(self):
        return self._password is not None

    def public_key_auth_supported(self):
        return self._public_key is not None

    def validate_password(self, username, password):
        return username == self._username and password == self._password

    def validate_public_key(self, username, key):
        return True

    async def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
        return await self._conn.forward_connection(dest_host, dest_port)

    def resize(self, fd, size):
        utils.logger.info(
            "[%s] Terminal window resize to (%d, %d)"
            % (self.__class__.__name__, size[0], size[1])
        )
        if sys.platform == "win32":
            # TODO
            pass
        else:
            import fcntl
            import termios

            winsize = struct.pack("HHHH", size[1], size[0], 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)

    async def _create_process(self, interactive, command, size):
        if not interactive:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                close_fds=True,
            )
            stdin = proc.stdin
            stdout = proc.stdout
            stderr = proc.stderr
        else:
            if sys.platform == "win32":
                if self._pty_enabled:
                    cmd = (
                        "conhost.exe",
                        "--headless",
                        "--width",
                        str(size[0]),
                        "--height",
                        str(size[1]),
                        "--",
                        command or "cmd.exe",
                    )
                else:
                    cmd = (command or "cmd.exe",)
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdin = proc.stdin
                stdout = proc.stdout
                stderr = proc.stderr
            else:
                import pty

                cmdline = list(shlex.split(command or os.environ.get("SHELL", "sh")))
                exe = cmdline[0]
                if exe[0] != "/":
                    for it in os.environ["PATH"].split(":"):
                        path = os.path.join(it, exe)
                        if os.path.isfile(path):
                            exe = path
                            break

                pid, fd = pty.fork()
                if pid == 0:
                    # child process
                    sys.stdout.flush()
                    try:
                        os.execve(exe, cmdline, os.environ)
                    except Exception as e:
                        sys.stderr.write(str(e))
                else:
                    self.resize(fd, size)
                    proc = utils.Process(pid)
                    stdin = utils.AsyncFileDescriptor(fd)
                    stdout = utils.AsyncFileDescriptor(fd)
                    stderr = None
        return proc, stdin, stdout, stderr

    def _handle_output(self, buffer):
        if self._line_editor:
            return buffer.decode("utf-8")
        else:
            return buffer

    async def _handle_process(self, process):
        username = process.get_extra_info("username")
        interactive = process.get_terminal_type() is not None
        utils.logger.info(
            "[%s] Create %sprocess %s"
            % (
                self.__class__.__name__,
                "interactive " if interactive else "",
                process.command or "",
            )
        )
        width, height, _, _ = process.get_terminal_size()
        proc, stdin, stdout, stderr = await self._create_process(
            interactive, process.command, (width, height)
        )
        if interactive:
            process.stdout.write(self._handle_output(self.barrier + b"\r\n"))
            process.stdout.write(self._handle_output(self.welcome))
            if not self._pty_enabled:
                process.stdout.write(
                    self._handle_output(
                        b"PTY not supported on %s, limited capability supported\r\n\r\n"
                        % platform.platform().encode()
                    )
                )
            process.stdout.write(self._handle_output(self.barrier + b"\r\n\r\n"))

        tasks = [None, None]
        if stderr:
            tasks.append(None)

        while proc.returncode is None:
            if tasks[0] is None:
                tasks[0] = utils.safe_ensure_future(process.stdin.read(4096))
            if tasks[1] is None:
                tasks[1] = utils.safe_ensure_future(stdout.read(4096))
            if stderr and tasks[2] is None:
                tasks[2] = utils.safe_ensure_future(stderr.read(4096))

            done_tasks, _ = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )

            for task in done_tasks:
                index = tasks.index(task)
                assert index >= 0
                tasks[index] = None
                try:
                    buffer = task.result()
                except asyncssh.BreakReceived:
                    return -1
                except asyncssh.TerminalSizeChanged as e:
                    self.resize(stdout, (e.width, e.height))
                    continue

                if not buffer:
                    return -1

                if index == 0:
                    if not isinstance(buffer, bytes):
                        buffer = buffer.encode("utf-8")
                    if buffer.endswith(b"\r") and sys.platform == "win32":
                        buffer += b"\n"

                    stdin.write(buffer)
                else:
                    if not self._pty_enabled:
                        for encoding in ("utf-8", "gbk"):
                            try:
                                buffer = buffer.decode(encoding)
                            except:
                                continue
                            else:
                                buffer = buffer.encode("utf8")
                                break
                    if index == 1:
                        process.stdout.write(self._handle_output(buffer))
                    else:
                        buffer = buffer.replace(b"\r\n", b"\r").replace(b"\r", b"\r\n")
                        process.stderr.write(self._handle_output(buffer))

        return proc.returncode

    async def handle_process(self, process):
        try:
            process.exit(await self._handle_process(process))
        except Exception as e:
            utils.logger.exception(
                "[%s] Create process %s failed"
                % (self.__class__.__name__, process.command)
            )
            message = (e.args and e.args[0]) or e.__class__.__name__
            process.stderr.write("Error: %s" % message)
            process.exit(-1)

    async def start(self):
        if self._line_editor:
            utils.logger.info("[%s] Line editor mode enabled" % self.__class__.__name__)
        try:
            self._conn = await asyncssh.create_server(
                lambda: MicroSSHServer(
                    self._listen_address,
                    self._server_host_keys,
                    self._username,
                    self._password,
                    self._public_key,
                ),
                self._listen_address[0],
                self._listen_address[1],
                server_host_keys=self._server_host_keys,
                process_factory=lambda process: asyncio.ensure_future(
                    self.handle_process(process)
                ),
                encoding="utf-8" if self._line_editor else None,
                line_editor=self._line_editor,
            )
        except OSError as e:
            utils.logger.error(
                "[%s] SSH server listen on %s:%d failed: %s"
                % (
                    self.__class__.__name__,
                    self._listen_address[0],
                    self._listen_address[1],
                    e,
                )
            )
        else:
            utils.logger.info(
                "[%s] SSH server is listening on %s:%d"
                % (
                    self.__class__.__name__,
                    self._listen_address[0],
                    self._listen_address[1],
                )
            )

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


class SSHTunnelServer(server.TunnelServer, MicroSSHServer):
    """SSH Tunnel Server"""

    def post_init(self):
        if not self._listen_url.auth:
            raise ValueError("SSH username not specified")
        if ":" in self._listen_url.auth:
            username, password = self._listen_url.auth.split(":", 1)
        else:
            username = self._listen_url.auth
            password = None
        public_key_path = self._listen_url.params.get("public_key")
        private_key_path = self._listen_url.params.get("private_key")
        if not private_key_path:
            private_key_path = "skey"
            if not os.path.isfile(private_key_path):
                skey = asyncssh.generate_private_key("ssh-rsa")
                skey.write_private_key(private_key_path)
                skey.write_public_key(private_key_path + ".pub")
        private_key_path = os.path.abspath(private_key_path)
        if not os.path.isfile(private_key_path):
            raise ValueError("Private key file %s not exist" % private_key_path)
        if public_key_path:
            public_key_path = os.path.abspath(public_key_path)
            if not os.path.isfile(public_key_path):
                raise ValueError("Public key file %s not exist" % public_key_path)

        MicroSSHServer.__init__(
            self,
            self._listen_url.address,
            [private_key_path],
            username,
            password,
            public_key_path,
        )

    def start(self):
        if sys.platform == "win32":
            loop = asyncio.ProactorEventLoop()
            asyncio.set_event_loop(loop)
        asyncio.ensure_future(MicroSSHServer.start(self))


class SSHClientConnection(asyncssh.SSHClientConnection):
    async def kbdint_challenge_received(self, name, instructions, lang, prompts):
        """Return responses to a keyboard-interactive auth challenge"""
        if not prompts:
            return []
        return [self._options.password]


class SSHTunnel(tunnel.Tunnel):
    """SSH Tunnel"""

    ssh_conns = {}

    def __init__(self, tunnel, url, address):
        super(SSHTunnel, self).__init__(tunnel, url, address)
        self._reader = None
        self._writer = None
        self._closed = False

    @classmethod
    def has_cache(cls, url):
        key = "%s:%d" % url.address
        if key in cls.ssh_conns:
            conn = cls.ssh_conns[key]
            if not conn._transport or conn._transport.closed():
                utils.logger.warn(
                    "[%s] SSH connection %s closed, remove cache" % (cls.__name__, key)
                )
                cls.ssh_conns.pop(key)
                return False
            return True
        return False

    async def create_ssh_conn(self):
        key = "%s:%d" % (self._url.address)
        if key not in self.__class__.ssh_conns:
            loop = asyncio.get_event_loop()
            options = {
                "known_hosts": None,
                "host": self._url.host,
                "port": self._url.port,
            }
            private_key_path = self._url.params.get("private_key")
            if private_key_path:
                if not os.path.isfile(private_key_path):
                    utils.logger.error(
                        "[%s] Private key file %s not found"
                        % (self.__class__.__name__, private_key_path)
                    )
                    return None
                options["client_keys"] = [private_key_path]
            if self._url.auth:
                password = None
                if ":" in self._url.auth:
                    username, password = self._url.auth.split(":", 1)
                else:
                    username = self._url.auth
                options["username"] = username

                if password:
                    if private_key_path:
                        options["passphrase"] = password
                    else:
                        options["password"] = password
                else:
                    options["password"] = ""
            try:
                options = asyncssh.SSHClientConnectionOptions(**options)
            except (asyncssh.KeyImportError, asyncssh.KeyEncryptionError) as e:
                utils.logger.error(
                    "[%s] Import private key %s failed: %s"
                    % (self.__class__.__name__, private_key_path, e)
                )
                return None

            utils.logger.info(
                "[%s] Create connection to ssh server %s:%d"
                % (self.__class__.__name__, self._url.host, self._url.port)
            )
            ssh_conn = SSHClientConnection(loop, options, wait="auth")
            transport = tunnel.TunnelTransport(self._tunnel, ssh_conn)
            ssh_conn.connection_made(transport)
            try:
                await ssh_conn.wait_established()
            except asyncssh.PermissionDenied as e:
                utils.logger.error(
                    "[%s] Connect ssh server %s:%d auth failed: %s"
                    % (self.__class__.__name__, self._url.host, self._url.port, e)
                )
                ssh_conn.abort()
                await ssh_conn.wait_closed()
                return None
            else:
                ssh_conn.set_keepalive(10)
            self.__class__.ssh_conns[key] = ssh_conn
        return self.__class__.ssh_conns[key]

    async def connect(self):
        ssh_conn = await self.create_ssh_conn()
        if not ssh_conn:
            return False
        try:
            self._reader, self._writer = await asyncio.wait_for(
                ssh_conn.open_connection(self._addr, self._port), self.__class__.timeout
            )
        except asyncssh.ChannelOpenError as e:
            utils.logger.warn(
                "[%s] Connect %s:%d over %s failed: %s"
                % (self.__class__.__name__, self._addr, self._port, self._url, e)
            )
            return False
        except asyncio.TimeoutError:
            utils.logger.warn(
                "[%s] Connect %s:%d over %s timeout"
                % (self.__class__.__name__, self._addr, self._port, self._url)
            )
            return False
        else:
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

    def closed(self):
        return self._reader is None or self._writer is None

    def close(self):
        if self._writer:
            self._writer.write_eof()
            self._writer = None
        self._reader = None

    async def fork(self):
        time0 = time.time()
        tunnel = self.__class__(self._tunnel, self._url, (self._addr, self._port))
        if await tunnel.connect():
            utils.logger.info(
                "[%s][%.3f] %s fork success"
                % (self.__class__.__name__, (time.time() - time0), tunnel)
            )
            return tunnel
        return None


class SSHProcessTunnel(SSHTunnel):
    """SSH Tunnel Over Process StdIn and StdOut"""

    def __init__(self, tunnel, url, address):
        super(SSHProcessTunnel, self).__init__(tunnel, url, address)
        self._process = None

    @classmethod
    def has_cache(cls, url):
        return False

    async def _log_stderr(self):
        while not self.closed():
            error_line = await self._process.stderr.readline()
            error_line = error_line.strip()
            utils.logger.warn(
                "[%s][stderr] %s" % (self.__class__.__name__, error_line.decode())
            )
            await asyncio.sleep(0.5)
        self._process = None

    async def connect(self):
        ssh_conn = await self.create_ssh_conn()
        if not ssh_conn:
            return False
        bin_path = self._url.path
        cmdline = "%s %s %d" % (bin_path, self._addr, self._port)
        self._process = await ssh_conn.create_process(cmdline, encoding=None)
        await asyncio.sleep(0.5)

        if self._process.exit_status is not None and self._process.exit_status != 0:
            utils.logger.error(
                "[%s] Create process %s failed: [%d]%s"
                % (
                    self.__class__.__name__,
                    cmdline,
                    self._process.exit_status,
                    await self._process.stderr.read(),
                )
            )
            return False
        status_line = await self._process.stderr.readline()
        if status_line.startswith(b"[OKAY]"):
            utils.safe_ensure_future(self._log_stderr())
            return True
        elif status_line.startswith(b"[FAIL]"):
            utils.logger.warn(
                "[%s] Connect %s:%d failed: %s"
                % (
                    self.__class__.__name__,
                    self._addr,
                    self._port,
                    status_line.decode(),
                )
            )
            return False
        else:
            raise RuntimeError("Unexpected stderr: %s" % status_line.decode())

    async def read(self):
        if self._process:
            buffer = await self._process.stdout.read(4096)
            if buffer:
                return buffer
        raise utils.TunnelClosedError()

    async def write(self, buffer):
        if self._process:
            return self._process.stdin.write(buffer)
        else:
            raise utils.TunnelClosedError()

    def closed(self):
        return self._process is None or self._process.exit_status is not None

    def close(self):
        if self._process:
            self._process.stdin.write(b"\x03")


registry.tunnel_registry.register("ssh", SSHTunnel)
registry.tunnel_registry.register("ssh+process", SSHProcessTunnel)
registry.server_registry.register("ssh", SSHTunnelServer)
