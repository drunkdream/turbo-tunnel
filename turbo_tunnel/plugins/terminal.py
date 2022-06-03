# -*- coding: utf-8 -*-
'''terminal plugin
'''

import asyncio
import logging
import sys
import time

from . import Plugin
from .. import BANNER
from ..registry import plugin_registry
from ..utils import logger


class TerminalColumn(object):
    def __init__(self, title, width, align='left', color=None):
        self.title = title
        self.width = width
        self.align = align
        self.color = color
        self.start = 0
        self.end = 0


class TerminalTable(object):
    def __init__(self, title, headers, stdout=None):
        self._title = title.strip()
        self._headers = []
        for it in headers:
            self._headers.append(TerminalColumn(**it))
        offset = 0
        for header in self._headers:
            header.start = offset
            offset += header.width
            header.end = offset
        self._stdout = stdout or sys.stdout
        self._stdout.write('\x1b[?1047h')

    def __del__(self):
        self._stdout.write('\x1b[?1047l')
        self._stdout.write('\x1b[100B')

    def render_text(self, text, line, column, color=None):
        if color:
            self._stdout.write(color)
        self._stdout.write('\x1b[%d;%dH' % (line, column))
        self._stdout.write(text)
        self._stdout.flush()

    def render_row(self, data, line, color=None):
        self._stdout.write('\x1b[0m')
        if color:
            self._stdout.write(color)

        for i, it in enumerate(data):
            header = self._headers[i]
            offset = header.start
            it = str(it)
            text = it
            if header.align != 'right':
                if len(it) > header.width:
                    text = it[:header.width]
            else:
                if len(it) > header.width:
                    text = it[len(it) - header.width:]
                else:
                    offset += header.width - len(it)
            self.render_text(text, line, offset)
        self._stdout.write('\x1b[0m')

    def render_headers(self, line):
        for header in self._headers:
            self.render_text(
                header.title, line, header.start if header.align == 'left' else
                (header.end - len(header.title)))

    def render(self, data_table):
        self._stdout.write('\x1b[2J')
        self.render_text(self._title, 1, 1)
        line = len(self._title.splitlines()) + 2
        self.render_headers(line)
        for index, data in enumerate(data_table):
            color = None
            if len(data) > len(self._headers):
                color = data[len(self._headers)]
            data = data[:len(self._headers)]
            self.render_row(data, index + line + 1, color)


class Connection(object):
    def __init__(self, src_addr, dst_addr, tun_addr=None):
        self._src_addr = src_addr
        self._dst_addr = dst_addr
        self._tun_addr = tun_addr
        self._start_time = time.time()
        self._end_time = None
        self._bytes_recv = 0
        self._bytes_sent = 0

    @property
    def client_address(self):
        return self._src_addr

    @property
    def target_address(self):
        return self._dst_addr

    @property
    def tunnel_address(self):
        return self._tun_addr

    @property
    def start_time(self):
        return time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(self._start_time))

    @property
    def duration(self):
        delta = int((self._end_time or time.time()) - self._start_time)
        second = delta % 60
        minute = delta // 60
        hour = minute // 60
        minute = minute % 60
        return '%.2d:%.2d:%.2d' % (hour, minute, second)

    @property
    def end_time(self):
        return self._end_time

    @property
    def bytes_sent(self):
        return self._bytes_sent

    @property
    def bytes_recv(self):
        return self._bytes_recv

    def on_tunnel_address_updated(self, tun_addr):
        self._tun_addr = tun_addr

    def on_close(self):
        self._end_time = time.time()

    def on_recv_bytes(self, bytes):
        self._bytes_recv += bytes

    def on_send_bytes(self, bytes):
        self._bytes_sent += bytes


class RedirectedOutStream(object):
    def write(self, s):
        file_path = None
        for handler in logger.handlers:
            if isinstance(handler, logging.FileHandler):
                file_path = handler.baseFilename
                break
        if file_path:
            with open(file_path, 'a') as fp:
                fp.write(s)

    def flush(self):
        pass


class TerminalPlugin(Plugin):
    '''Show connections in terminal
    '''
    flush_internal = 1
    conn_opened_color = '\x1b[32m'
    conn_closed_color = '\x1b[1;31m'

    def _patch_output(self):
        origin_stdout = sys.stdout
        origin_stderr = sys.stderr
        sys.stdout = RedirectedOutStream()
        sys.stderr = RedirectedOutStream()
        for handler in logger.handlers:
            handler.stream = sys.stdout
        return origin_stdout, origin_stderr

    def on_load(self):
        origin_stdout, _ = self._patch_output()
        self._term_tab = TerminalTable(BANNER, [{
            'title': 'Source Address',
            'width': 18,
            'align': 'left',
        }, {
            'title': 'Tunnel Address',
            'width': 22,
            'align': 'left'
        }, {
            'title': 'Target Address',
            'width': 22,
            'align': 'left'
        }, {
            'title': 'Start Time',
            'width': 20,
            'align': 'left'
        }, {
            'title': 'Duration',
            'width': 10,
            'align': 'right'
        }, {
            'title': 'Bytes Out',
            'width': 11,
            'align': 'right'
        }, {
            'title': 'Bytes In',
            'width': 11,
            'align': 'right'
        }], origin_stdout)
        self._conn_list = []
        self._running = True
        asyncio.ensure_future(self.run())

    def on_unload(self):
        self._running = False
        del self._term_tab
        self._term_tab = None

    def _lookup_connection(self, src_addr, dst_addr):
        for conn in self._conn_list:
            if conn.client_address == src_addr and conn.target_address == dst_addr:
                return conn
        logger.warn('[%s] Connection %s:%d => %s:%d not found' %
                    (self.__class__.__name__, src_addr[0], src_addr[1],
                     dst_addr[0], dst_addr[1]))
        return None

    def on_new_connection(self, connection):
        conn = Connection(connection.client_address, connection.target_address,
                          connection.tunnel_address)
        self._conn_list.append(conn)

    def on_tunnel_address_updated(self, connection, tunnel_address):
        conn = self._lookup_connection(connection.client_address,
                                       connection.target_address)
        if conn:
            conn.on_tunnel_address_updated(tunnel_address)

    def on_data_recevied(self, connection, buffer):
        conn = self._lookup_connection(connection.client_address,
                                       connection.target_address)
        if conn:
            conn.on_recv_bytes(len(buffer))

    def on_data_sent(self, connection, buffer):
        conn = self._lookup_connection(connection.client_address,
                                       connection.target_address)
        if conn:
            conn.on_send_bytes(len(buffer))

    def on_connection_closed(self, connection):
        conn = self._lookup_connection(connection.client_address,
                                       connection.target_address)
        if conn:
            conn.on_close()

    async def run(self):
        prev_conns = []
        closed_conns = []
        while self._running:
            data_table = []
            if closed_conns:
                for conn in closed_conns:
                    closed_conns.remove(conn)
                    if conn in self._conn_list:
                        self._conn_list.remove(conn)
                    if conn in prev_conns:
                        prev_conns.remove(conn)

            for conn in self._conn_list:
                data = [
                    '%s:%d' % conn.client_address,
                    ('%s:%d' %
                     conn.tunnel_address) if conn.tunnel_address else '--',
                    '%s:%d' % conn.target_address, conn.start_time,
                    conn.duration, conn.bytes_sent, conn.bytes_recv
                ]
                if not conn in prev_conns:
                    data.append(self.conn_opened_color)
                    prev_conns.append(conn)
                elif conn.end_time:
                    data.append(self.conn_closed_color)
                    closed_conns.append(conn)

                data_table.append(data)

            self._term_tab.render(data_table)
            await asyncio.sleep(self.flush_internal)


plugin_registry.register(TerminalPlugin)
