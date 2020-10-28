# -*- coding: utf-8 -*-

import subprocess
import time

from .util import get_random_port


def test_port_forward():
    cmdline = (
        "python -m turbo_tunnel -l tcp://127.0.0.1:%d -t tcp://127.0.0.1:8888"
        % get_random_port()
    )
    proc = subprocess.Popen(
        cmdline, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True
    )
    time.sleep(2)
    assert proc.poll() is None
    proc.terminate()
