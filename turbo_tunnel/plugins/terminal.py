# -*- coding: utf-8 -*-
"""terminal plugin
"""

import asyncio
import atexit
import curses
import math
import sys
import time
import traceback

from . import Plugin
from .. import BANNER, VERSION
from ..registry import plugin_registry
from ..utils import logger


curses_colors = {}


class TerminalScreen(object):
    """Terminal Screen"""

    def __init__(self, handler=None):
        self._handler = handler
        self._stdscr = curses.initscr()
        self._stdscr.scrollok(True)
        self._height, self._width = self._stdscr.getmaxyx()
        curses.start_color()
        curses.use_default_colors()
        self._views = []
        self._running = True
        asyncio.ensure_future(self.check_screen_size_task())
        # atexit.register(lambda: curses.endwin())

    @property
    def width(self):
        return self._width

    @property
    def height(self):
        return self._height

    def __del__(self):
        self._running = False
        try:
            curses.endwin()
        except:
            pass

    def create_view(self, width, height, pos=None):
        pos = pos or (0, 0)
        if width <= 0:
            width = self._width
        if height >= self._height:
            height = self._height
        view = self._stdscr.subpad(height, width, pos[1], pos[0])
        view.scrollok(1)
        view = TerminalView(view, width, height)
        self._views.append(view)
        return view

    async def check_screen_size_task(self):
        while self._running:
            height, width = self._stdscr.getmaxyx()
            if width != self._width or height != self._height:
                if self._handler:
                    self._handler.on_screen_size_changed(width, height)
                for view in self._views:
                    cb = getattr(view, "on_screen_size_changed", None)
                    if cb:
                        view.on_screen_size_changed(width, height)
            self._width, self._height = width, height
            await asyncio.sleep(0.1)


class TerminalView(object):
    """Terminal View"""

    def __init__(self, view, width, height):
        self._view = view
        self._width = width
        self._height = height
        self._last_pos = [0, 0]
        self._last_color = None
        self._buffer = []

    @property
    def width(self):
        return self._width

    @property
    def height(self):
        return self._height

    def move(self, pos):
        self.clear()
        self.refresh()
        try:
            self._view.mvwin(pos[1], pos[0])
            self._view.mvderwin(pos[1], pos[0])
        except:
            logger.exception(
                "[%s] Move terminal view to %d,%d failed"
                % (self.__class__.__name__, pos[0], pos[1])
            )
            return

        for i in range(self._height):
            row = len(self._buffer) - self._height + i
            if row < 0 or len(self._buffer) <= row:
                continue
            for j in range(min(len(self._buffer[row]), self._width)):
                c, color = self._buffer[row][j]
                try:
                    if color is not None:
                        self._view.addstr(i, j, c, color)
                    else:
                        self._view.addstr(i, j, c)
                except:
                    continue
                    # raise Exception("Write to (%d,%d) failed" % (j, i))
        self.refresh()

    def resize(self, width=None, height=None):
        width = width or self._width
        height = height or self._height
        try:
            self._view.resize(height, width)
        except:
            logger.exception(
                "[%s] Resize terminal view to %d:%d failed"
                % (self.__class__.__name__, width, height)
            )
            return
        if self._last_pos[1] >= height:
            self.scroll(self._last_pos[1] - height)
            self._last_pos[1] = height - 1
        self._width = width
        self._height = height
        self._view.refresh()

    def get_color(self, color):
        if color in curses_colors:
            return curses_colors[color]
        index = len(curses_colors) + 1
        s_color = color
        if ";" in color:
            color = color.split(";")[-1]
        color = int(color)

        if color == 0:
            return None
        elif color >= 30 and color < 38:
            curses.init_pair(index, color - 30, -1)
        elif color >= 40 and color < 48:
            curses.init_pair(index, -1, color - 40)
        else:
            return None
        curses_colors[s_color] = curses.color_pair(index)
        return curses_colors[s_color]

    def parse_string_format(self, line):
        items = []
        i = j = 0
        color = None
        while i <= len(line):
            if i == len(line):
                items.append((color, line[j:i]))
                break
            elif line[i] == "\x1b" and line[i + 1] == "[":
                if i > 0:
                    items.append((color, line[j:i]))
                j = line.find("m", i) + 1
                color = line[i + 2 : j - 1]
                i = j
            else:
                i += 1
        return items

    def clear(self):
        self._view.erase()

    def scroll(self, rows):
        self._view.scroll(rows)
        for i in range(self._height - rows - 1):
            self._buffer[i] = self._buffer[i + rows][:]

    def _set_buffer(self, x, y, data):
        if len(self._buffer) <= y:
            for _ in range(len(self._buffer), y + 1):
                self._buffer.append([(" ", None) for _ in range(self._width)])
        if len(self._buffer[y]) <= x:
            for _ in range(len(self._buffer[y]), x + 1):
                self._buffer[y].append((" ", None))
        self._buffer[y][x] = data

    def write_string(self, content, pos, color=None, auto_wrap=True):
        if not content:
            return
        if color is not None:
            color = self.get_color(color)
        if not auto_wrap:
            content_length = len(content)
            available_length = self._width - pos[0]
            if available_length <= 0:
                return
            elif available_length < content_length:
                content = content[:available_length]
        if pos[1] >= self._height:
            return

        for i, c in enumerate(content):
            x, y = pos[0] + i, pos[1]
            if x >= self._width:
                y += x // self._width
                x = x % self._width
            self._set_buffer(x, y, (c, color))

        if color is not None:
            self._view.addstr(pos[1], pos[0], content, color)
        else:
            self._view.addstr(pos[1], pos[0], content)

    def write_buffer(
        self, buffer, pos=None, append=True, auto_wrap=True, refresh=False
    ):
        if pos and pos[1] >= self._height:
            raise ValueError("Invalid position (%s, %s)" % pos)
        elif not pos and append:
            pos = self._last_pos
        elif not pos:
            pos = (0, 0)
        pos = list(pos)
        lines = buffer.split("\n")
        for i, line in enumerate(lines):
            if line:
                if pos[1] >= self._height:
                    self.scroll(1)
                    pos[1] -= 1
                items = self.parse_string_format(line)
                line_height = prev_line_height = 1
                line_bytes = pos[0]
                for color, content in items:
                    color = color or self._last_color
                    if content:
                        line_bytes += len(content)
                        line_height = math.ceil(line_bytes / self._width)
                        curr_pos = list(pos)
                        if line_height > prev_line_height:
                            line_height_delta = line_height - prev_line_height
                            if pos[1] >= self._height - line_height_delta:
                                self.scroll(line_height_delta - 1)
                                curr_pos[1] -= line_height_delta - 1
                            else:
                                pos[1] += line_height_delta
                        pos[0] = line_bytes % self._width
                        if pos[0] == 0:
                            pos[1] += 1

                    prev_line_height = line_height
                    self.write_string(content, curr_pos, color, auto_wrap=auto_wrap)
                    self._last_color = color

            if i < len(lines) - 1:
                if pos[1] < self._height:
                    pos[1] += 1
                pos[0] = 0
        self._last_pos = pos

        if refresh:
            self.refresh()

    def refresh(self):
        self._view.refresh()


class TerminalColumn(object):
    def __init__(self, title, width, align="left", color=None):
        self.title = title
        self.width = width
        self.align = align
        self.color = color
        self.start = 0
        self.end = 0


class TerminalTable(object):
    def __init__(self, title, headers, view):
        self._title = title.strip()
        self._headers = []
        for it in headers:
            self._headers.append(TerminalColumn(**it))
        offset = 0
        for header in self._headers:
            header.start = offset
            offset += header.width
            header.end = offset
        self._view = view

    def __del__(self):
        pass

    def render_text(self, text, line, column, color=None):
        if line >= self._view.height:
            return
        if color:
            text = color + text + "\x1b[0m"
        self._view.write_buffer(text, (column, line), auto_wrap=False)

    def render_row(self, data, line, color=None):
        for i, it in enumerate(data):
            header = self._headers[i]
            offset = header.start
            it = str(it)
            text = it
            if header.align != "right":
                if len(it) > header.width:
                    text = it[: header.width]
            else:
                if len(it) > header.width:
                    text = it[len(it) - header.width :]
                elif len(it) < (header.end - header.start):
                    offset += header.width - len(it)
            self.render_text(text, line, offset, color)

    def render_headers(self, line):
        for header in self._headers:
            self.render_text(
                header.title,
                line,
                header.start
                if header.align == "left"
                else (header.end - len(header.title)),
            )

    def render(self, data_table):
        self._view.clear()
        self.render_text(self._title, 0, 0)
        line_count = len(self._title.split("\n"))
        self.render_headers(line_count + 1)
        for index, data in enumerate(data_table):
            color = None
            if len(data) > len(self._headers):
                color = data[len(self._headers)]
            data = data[: len(self._headers)]
            self.render_row(data, index + line_count + 2, color)
        self._view.refresh()


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
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._start_time))

    @property
    def duration(self):
        delta = int((self._end_time or time.time()) - self._start_time)
        second = delta % 60
        minute = delta // 60
        hour = minute // 60
        minute = minute % 60
        return "%.2d:%.2d:%.2d" % (hour, minute, second)

    @property
    def end_time(self):
        return self._end_time

    @property
    def bytes_sent(self):
        if self._bytes_sent < 1024:
            return self._bytes_sent
        elif self._bytes_sent < 1024 * 1024:
            return "%.2fK" % (self._bytes_sent / 1024)
        else:
            return "%.2fM" % (self._bytes_sent / (1024 * 1024))

    @property
    def bytes_recv(self):
        if self._bytes_recv < 1024:
            return self._bytes_recv
        elif self._bytes_recv < 1024 * 1024:
            return "%.2fK" % (self._bytes_recv / 1024)
        else:
            return "%.2fM" % (self._bytes_recv / (1024 * 1024))

    def on_tunnel_address_updated(self, tun_addr):
        self._tun_addr = tun_addr

    def on_close(self):
        self._end_time = time.time()

    def on_recv_bytes(self, bytes):
        self._bytes_recv += bytes

    def on_send_bytes(self, bytes):
        self._bytes_sent += bytes


class TerminalLogger(object):
    """Terminal Logger"""

    def __init__(self, view, file=None):
        self._view = view
        self._file = file
        self._running = True

    def write(self, buffer):
        if not self._running:
            return
        try:
            self._view.write_buffer(buffer, refresh=True)
        except:
            self._running = False
            if self._file:
                self._file.write(
                    "[%s] Write terminal logger failed\n%s"
                    % (self.__class__.__name__, traceback.format_exc())
                )

    def flush(self):
        pass


class TerminalPlugin(Plugin):
    """Show connections in terminal"""

    flush_internal = 1
    conn_opened_color = "\x1b[32m"
    conn_closed_color = "\x1b[1;31m"

    def on_screen_size_changed(self, width, height):
        table_height, log_height = self._get_view_height(height)
        if height > self._screen.height:
            self._log_view.move((0, table_height))
            self._table_view.resize(width, table_height)
            self._log_view.resize(width, log_height)
        else:
            self._table_view.resize(width, table_height)
            self._log_view.move((0, table_height))
            self._log_view.resize(width, log_height - 1)

    def _patch_output(self, view):
        origin_stdout = sys.stdout
        origin_stderr = sys.stderr
        sys.stdout = sys.stderr = TerminalLogger(view, origin_stderr)
        for handler in logger.handlers:
            handler.stream = sys.stdout
        return origin_stdout, origin_stderr

    def _get_view_height(self, screen_height):
        min_table_view_height = 15
        max_log_view_height = 15
        table_height = screen_height - max_log_view_height
        if table_height < min_table_view_height:
            table_height = min_table_view_height
        return table_height, screen_height - table_height

    def on_load(self):
        self._screen = TerminalScreen(self)
        table_height, log_height = self._get_view_height(self._screen.height)
        self._table_view = self._screen.create_view(self._screen.width, table_height)
        self._log_view = self._screen.create_view(
            self._screen.width, log_height, (0, table_height)
        )
        self._origin_stdout, self._origin_stderr = self._patch_output(self._log_view)
        self._term_tab = TerminalTable(
            "\x1b[36m%s \x1b[32mv%s\x1b[0m" % (BANNER.lstrip("\n").rstrip(), VERSION),
            [
                {
                    "title": "Source Address",
                    "width": 18,
                    "align": "left",
                },
                {"title": "Tunnel Address", "width": 24, "align": "left"},
                {"title": "Target Address", "width": 32, "align": "left"},
                {"title": "Start Time", "width": 20, "align": "left"},
                {"title": "Duration", "width": 10, "align": "right"},
                {"title": "Bytes Out", "width": 11, "align": "right"},
                {"title": "Bytes In", "width": 10, "align": "right"},
            ],
            self._table_view,
        )

        self._conn_list = []
        self._running = True
        asyncio.ensure_future(self.run())

    def on_unload(self):
        self._running = False
        if getattr(self, "_origin_stdout", None):
            sys.stdout = self._origin_stdout
        if getattr(self, "_origin_stderr", None):
            sys.stderr = self._origin_stderr
        curses.endwin()
        if getattr(self, "_term_tab", None):
            del self._term_tab
            self._term_tab = None
        if getattr(self, "_screen", None):
            del self._screen
            self._screen = None

    def _lookup_connection(self, src_addr, dst_addr):
        for conn in self._conn_list:
            if conn.client_address == src_addr and conn.target_address == dst_addr:
                return conn
        logger.warn(
            "[%s] Connection %s:%d => %s:%d not found"
            % (
                self.__class__.__name__,
                src_addr[0],
                src_addr[1],
                dst_addr[0],
                dst_addr[1],
            )
        )
        return None

    def on_new_connection(self, connection):
        conn = Connection(
            connection.client_address,
            connection.target_address,
            connection.tunnel_address,
        )
        self._conn_list.append(conn)

    def on_tunnel_address_updated(self, connection, tunnel_address):
        conn = self._lookup_connection(
            connection.client_address, connection.target_address
        )
        if conn:
            conn.on_tunnel_address_updated(tunnel_address)

    def on_data_recevied(self, connection, buffer):
        conn = self._lookup_connection(
            connection.client_address, connection.target_address
        )
        if conn:
            conn.on_recv_bytes(len(buffer))

    def on_data_sent(self, connection, buffer):
        conn = self._lookup_connection(
            connection.client_address, connection.target_address
        )
        if conn:
            conn.on_send_bytes(len(buffer))

    def on_connection_closed(self, connection):
        conn = self._lookup_connection(
            connection.client_address, connection.target_address
        )
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
                    "%s:%d" % conn.client_address,
                    ("%s:%d" % conn.tunnel_address)
                    if conn.tunnel_address
                    and conn.tunnel_address[0]
                    and conn.tunnel_address[1]
                    else "--",
                    "%s:%d" % conn.target_address,
                    conn.start_time,
                    conn.duration,
                    conn.bytes_sent,
                    conn.bytes_recv,
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
