# -*- coding: utf-8 -*-

import os
import sys

try:
    from turbo_tunnel.plugins import terminal
except:
    terminal = None


def test_terminal_view():
    if terminal is None or os.environ.get("TERM", "unknown") in ("unknown", "dumb"):
        return
    screen = terminal.TerminalScreen()
    view = screen.create_view(screen.width, 15, (0, screen.height - 15))
    for i in range(14):
        view.write_buffer(
            "\x1b[%dm%s\x1b[0m<===\n" % (30 + i, str(i) * 50), refresh=True
        )
