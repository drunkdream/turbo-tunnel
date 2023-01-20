# -*- coding: utf-8 -*-

"""Turbo tunnel
"""

VERSION = "0.17.1"
BANNER = r"""
 _____            _          _____                        _
/__   \_   _ _ __| |__   ___/__   \_   _ _ __  _ __   ___| |
  / /\/ | | | '__| '_ \ / _ \ / /\/ | | | '_ \| '_ \ / _ \ |
 / /  | |_| | |  | |_) | (_) / /  | |_| | | | | | | |  __/ |
 \/    \__,_|_|  |_.__/ \___/\/    \__,_|_| |_|_| |_|\___|_|
"""

import sys
import traceback

try:
    from . import https
    from . import icmp
    from . import k8s
    from . import server
    from . import socks

    if sys.version_info[1] >= 6:
        # ssh disabled in python 3.5
        from . import ssh
    from . import tunnel
    from . import websocket
except ImportError:
    traceback.print_exc()
