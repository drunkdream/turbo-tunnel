# -*- coding: utf-8 -*-
"""Miscellaneous utility functions and classes
"""

import asyncio
import inspect
import logging
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse

import async_dns.core
import async_dns.core.config
import async_dns.resolver
import tornado.httpclient
import tornado.iostream
import tornado.netutil

logger = logging.getLogger("turbo-tunnel")


def verbose_logging(msg):
    if logger.level < logging.DEBUG:
        return logger.debug(msg)


logging._nameToLevel["VERBOSE"] = 5
logging._levelToName[5] = "VERBOSE"
logger.verbose = verbose_logging


class Url(object):
    def __init__(self, url):
        self._url = url
        obj = urllib.parse.urlparse(url)
        self._protocol = obj.scheme
        netloc = obj.netloc
        auth = ""
        port = 0
        if "@" in netloc:
            auth, netloc = netloc.split("@")
        if ":" in netloc:
            netloc, port = netloc.split(":")
        self._auth, self._host, self._port = auth, netloc, int(port)
        if not self._host and self._port:
            self._host = "0.0.0.0"
        self._path = obj.path
        self._query = obj.query
        self._params = None

    def __str__(self):
        port = self.port or ""
        if self._protocol in ("http", "ws") and port == 80:
            port = ""
        elif self._protocol in ("https", "wss") and port == 443:
            port = ""
        elif self._protocol in ("ssh",) and port == 22:
            port = ""
        elif port:
            port = ":%d" % port
        url = "%s://%s%s%s" % (self._protocol, self._host, port, self._path)
        query = self.query
        if query:
            url += "?" + query
        return url

    def __eq__(self, other):
        if not other:
            return False
        if not isinstance(other, Url):
            other = Url(other)
        return (
            self.protocol == other.protocol
            and self.host == other.host
            and self.port == other.port
            and self.path == other.path
        )

    @property
    def protocol(self):
        return self._protocol

    @protocol.setter
    def protocol(self, proto):
        self._protocol = proto

    @property
    def host(self):
        return self._host

    @property
    def port(self):
        if self._port:
            return self._port
        if self.protocol in ("http", "ws"):
            return 80
        elif self.protocol in ("https", "wss"):
            return 443
        elif self.protocol in ("ssh",):
            return 22

    @property
    def address(self):
        return self.host, self.port

    @property
    def auth(self):
        return urllib.parse.unquote(self._auth)

    @property
    def path(self):
        return self._path

    @path.setter
    def path(self, value):
        self._path = value

    @property
    def query(self):
        if self._params is None:
            return self._query
        items = []
        for key in self._params:
            if isinstance(self._params[key], list):
                for it in self._params[key]:
                    items.append("%s=%s" % (key, urllib.parse.quote(it)))
            else:
                items.append(
                    "%s=%s" % (key, urllib.parse.quote(str(self._params[key])))
                )
        return "&".join(items)

    @property
    def params(self):
        if self._params is None:
            result = {}
            if self._query:
                items = self._query.split("&")
                for item in items:
                    if "=" not in item:
                        continue
                    key, value = item.split("=", 1)
                    if key in result:
                        if not isinstance(result[key], list):
                            result[key] = [result[key]]
                        result[key].append(urllib.parse.unquote(value))
                    else:
                        result[key] = urllib.parse.unquote(value)
            self._params = result
        return self._params


class Singleton(object):
    """Singleton Decorator"""

    def __init__(self, cls):
        self.__instance = None
        self.__cls = cls

    def __call__(self, *args, **kwargs):
        if not self.__instance:
            self.__instance = self.__cls(*args, **kwargs)
        return self.__instance


@Singleton
class AsyncTaskManager(object):
    def __init__(self):
        self._async_tasks = []
        self._sleep_task = None

    @property
    def running_tasks(self):
        return self._async_tasks

    async def wrap_task(self, task, cb_exc=None):
        self._async_tasks.append(task)
        logger.debug("[%s] Task %s start" % (self.__class__.__name__, task.__name__))
        try:
            await task
        except asyncio.CancelledError:
            logger.warning(
                "[%s] Task %s force stopped" % (self.__class__.__name__, task.__name__)
            )
        except Exception as ex:
            if cb_exc:
                cb_exc(ex)
            else:
                logger.exception(
                    "[%s] Task %s crash" % (self.__class__.__name__, task.__name__)
                )
        else:
            logger.debug("[%s] Task %s exit" % (self.__class__.__name__, task.__name__))
        self._async_tasks.remove(task)

    def start_task(self, task, cb_exc=None):
        return asyncio.ensure_future(self.wrap_task(task, cb_exc))

    async def wait_for_tasks(self, tasks):
        """wait until one task complete"""
        task_list = [asyncio.ensure_future(self.wrap_task(task)) for task in tasks]
        await asyncio.wait(task_list, return_when=asyncio.FIRST_COMPLETED)
        # await asyncio.sleep(0.1)
        for i, task in enumerate(tasks):
            if not task in self._async_tasks:
                continue
            task_list[i].cancel()
            await asyncio.sleep(0)  # Wait for task exit

    async def sleep(self):
        if not self._sleep_task:
            # Avoid too many sleep tasks
            self._sleep_task = asyncio.sleep(0.001)
        await self._sleep_task
        self._sleep_task = None


class IStream(object):
    @property
    def socket(self):
        raise NotImplementedError(
            "%s.%s" % (self.__class__.__name__, inspect.currentframe().f_code.co_name)
        )

    @property
    def stream(self):
        raise NotImplementedError(
            "%s.%s" % (self.__class__.__name__, inspect.currentframe().f_code.co_name)
        )

    @property
    def target_address(self):
        raise NotImplementedError(
            "%s.%s" % (self.__class__.__name__, inspect.currentframe().f_code.co_name)
        )

    async def connect(self):
        raise NotImplementedError(
            "%s.%s" % (self.__class__.__name__, inspect.currentframe().f_code.co_name)
        )

    def closed(self):
        raise NotImplementedError(
            "%s.%s" % (self.__class__.__name__, inspect.currentframe().f_code.co_name)
        )

    async def read(self):
        raise NotImplementedError(
            "%s.%s" % (self.__class__.__name__, inspect.currentframe().f_code.co_name)
        )

    async def readline(self):
        raise NotImplementedError(
            "%s.%s" % (self.__class__.__name__, inspect.currentframe().f_code.co_name)
        )

    async def read_until(self, delimiter, timeout=None):
        raise NotImplementedError(
            "%s.%s" % (self.__class__.__name__, inspect.currentframe().f_code.co_name)
        )

    async def write(self, buffer):
        raise NotImplementedError(
            "%s.%s" % (self.__class__.__name__, inspect.currentframe().f_code.co_name)
        )

    def close(self):
        raise NotImplementedError(
            "%s.%s" % (self.__class__.__name__, inspect.currentframe().f_code.co_name)
        )

    async def fork(self):
        raise NotImplementedError(
            "%s.%s" % (self.__class__.__name__, inspect.currentframe().f_code.co_name)
        )


class ConfigError(RuntimeError):
    pass


class TimeoutError(RuntimeError):
    pass


class ResolveError(RuntimeError):
    pass


class TunnelError(RuntimeError):
    pass


class TunnelConnectError(TunnelError):
    pass


class TunnelBlockedError(TunnelError):
    pass


class TunnelClosedError(TunnelError):
    pass


class TunnelPacketError(TunnelError):
    pass


class TunnelPacketLengthError(TunnelPacketError):
    pass


class ParamError(RuntimeError):
    pass


class AsyncFileDescriptor(object):
    """Async File Descriptor"""

    def __init__(self, fd):
        self._loop = asyncio.get_event_loop()
        self._fd = fd
        self._event = asyncio.Event()
        self._buffer = bytearray()
        self._loop.add_reader(self._fd, self.read_callback)
        self._closed = False

    def fileno(self):
        return self._fd

    def close(self):
        self._loop.remove_reader(self._fd)

    async def read(self, bytes=4096):
        await self._event.wait()
        self._event.clear()
        buffer = self._buffer
        self._buffer = bytearray()
        return buffer

    def write(self, buffer):
        os.write(self._fd, buffer)

    def read_callback(self, *args):
        try:
            buffer = os.read(self._fd, 4096)
        except OSError:
            self.close()
            self._closed = True
            self._event.set()
            return

        self._buffer += buffer
        self._event.set()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_trackback):
        self.close()
        self._closed = True


class Process(object):
    def __init__(self, pid):
        self._pid = pid
        self._returncode = None
        asyncio.ensure_future(self._wait_for_exit())

    @property
    def returncode(self):
        return self._returncode

    async def _wait_for_exit(self):
        while True:
            try:
                pid, returncode = os.waitpid(self._pid, os.WNOHANG)
            except ChildProcessError:
                logger.warn(
                    "[%s] Process %d already exited" % (self.__class__.__name__, pid)
                )
                self._returncode = -1
                break
            if not pid:
                await asyncio.sleep(0.01)
            else:
                self._returncode = returncode
                break


def is_ip_address(addr):
    """check is valid v4/v6 ip"""
    patterns = (
        r"(25[0-5]|2[0-4]\d|[0-1]?\d?\d)(\.(25[0-5]|2[0-4]\d|[0-1]?\d?\d)){3}",
        r"^\s*((([0-9A-Fa-f]{1,4}:){7}([0-9A-Fa-f]{1,4}|:))|(([0-9A-Fa-f]{1,4}:){6}(:[0-9A-Fa-f]{1,4}|((25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3})|:))|(([0-9A-Fa-f]{1,4}:){5}(((:[0-9A-Fa-f]{1,4}){1,2})|:((25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3})|:))|(([0-9A-Fa-f]{1,4}:){4}(((:[0-9A-Fa-f]{1,4}){1,3})|((:[0-9A-Fa-f]{1,4})?:((25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}))|:))|(([0-9A-Fa-f]{1,4}:){3}(((:[0-9A-Fa-f]{1,4}){1,4})|((:[0-9A-Fa-f]{1,4}){0,2}:((25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}))|:))|(([0-9A-Fa-f]{1,4}:){2}(((:[0-9A-Fa-f]{1,4}){1,5})|((:[0-9A-Fa-f]{1,4}){0,3}:((25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}))|:))|(([0-9A-Fa-f]{1,4}:){1}(((:[0-9A-Fa-f]{1,4}){1,6})|((:[0-9A-Fa-f]{1,4}){0,4}:((25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}))|:))|(:(((:[0-9A-Fa-f]{1,4}){1,7})|((:[0-9A-Fa-f]{1,4}){0,5}:((25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}))|:)))(%.+)?\s*$",
    )
    for pattern in patterns:
        if re.match(pattern, addr):
            return True
    return False


def get_nameserver():
    name_servers = async_dns.core.config.get_nameservers()
    name_servers = [it for it in name_servers if "." in it]
    name_servers.extend(async_dns.core.config.core_config.get("default_nameservers"))
    return name_servers


def patch_async_dns():
    if sys.platform == "win32":
        from async_dns.request import udp
        from async_dns.core import types

        for ip_type in (types.A,):
            if not udp.Dispatcher.data.get(ip_type):
                udp.Dispatcher.data[ip_type] = udp.Dispatcher(
                    ip_type, ("0.0.0.0", random.randint(1024, 65535))
                )


name_servers = get_nameserver()
assert len(name_servers) > 0

resovle_timeout = 600
resolve_cache = {}


async def resolve_address(address):
    if not is_ip_address(address[0]):
        if (
            address in resolve_cache
            and time.time() - resolve_cache[address]["time"] <= resovle_timeout
        ):
            return resolve_cache[address]["result"]

        patch_async_dns()
        resolver = async_dns.resolver.DNSClient()
        result = address
        res = None
        for name_server in name_servers:
            try:
                res = await resolver.query(
                    address[0],
                    async_dns.core.types.A,
                    async_dns.core.Address.parse(name_server),
                )
            except asyncio.TimeoutError:
                pass
            else:
                break
        if res:
            addr_list = res.an
            for it in addr_list:
                if it.data.type_name == "a":
                    result = (it.data.data, address[1])
                    break
            resolve_cache[address] = {"time": time.time(), "result": result}
            return result
    return address


def safe_ensure_future(coro, loop=None):
    loop = loop or asyncio.get_event_loop()
    fut = loop.create_future()

    async def _wrap():
        try:
            fut.set_result(await coro)
        except Exception as e:
            fut.set_exception(e)

    asyncio.ensure_future(_wrap())
    return fut


def win32_daemon():
    cmdline = []
    for it in sys.argv:
        if it not in ("-d", "--daemon"):
            cmdline.append(it)

    DETACHED_PROCESS = 8
    subprocess.Popen(cmdline, creationflags=DETACHED_PROCESS, close_fds=True)


async def fetch(url):
    http_client = tornado.httpclient.AsyncHTTPClient()
    try:
        response = await http_client.fetch(url)
    except Exception:
        logger.exception("Fetch %s failed" % url)
        return None
    else:
        return response.body
    finally:
        http_client.close()


def checksum(data):
    s = 0
    n = len(data) % 2
    for i in range(0, len(data) - n, 2):
        s += data[i + 1] + (data[i] << 8)
    if n:
        s += data[i + 1] << 8

    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    s = ~s & 0xFFFF
    return s


def enable_native_ansi():
    """Enables native ANSI sequences in console. Windows 10 only.
    Returns whether successful.
    """
    import ctypes.wintypes

    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x04

    out_handle = ctypes.windll.kernel32.GetStdHandle(subprocess.STD_OUTPUT_HANDLE)

    # GetConsoleMode fails if the terminal isn't native.
    mode = ctypes.wintypes.DWORD()
    if ctypes.windll.kernel32.GetConsoleMode(out_handle, ctypes.byref(mode)) == 0:
        return False

    if not (mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING):
        if (
            ctypes.windll.kernel32.SetConsoleMode(
                out_handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            )
            == 0
        ):
            print(
                "kernel32.SetConsoleMode to enable ANSI sequences failed",
                file=sys.stderr,
            )
            return False

    return True
