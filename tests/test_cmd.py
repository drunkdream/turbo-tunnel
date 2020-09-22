# -*- coding: utf-8 -*-

import subprocess
import time


def test_port_forward():
    cmdline = 'python -m turbo_tunnel -l tcp://127.0.0.1:4444 -t tcp://127.0.0.1:8888'
    proc = subprocess.Popen(cmdline, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    time.sleep(2)
    assert(proc.poll() is None)
    proc.terminate()
