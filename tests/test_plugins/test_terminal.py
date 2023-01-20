# -*- coding: utf-8 -*-

import os
import sys

from turbo_tunnel.plugins import terminal


def test_terminal_view():
    if sys.platform == "win32" or os.environ.get("TERM", "unknown") in ("unknown", "dumb"):
        return
    screen = terminal.TerminalScreen()
    view = screen.create_view(screen.width, 15, (0, screen.height - 15))
    for i in range(14):
        view.write_buffer("\x1b[%dm%s\x1b[0m<===\n" % (30+i, str(i)*50), refresh=True)
