# -*- coding: utf-8 -*-

"""Kubernetes Tunnel
"""


import os
import time

from . import registry
from . import websocket
from . import utils


class KubernetesTunnel(websocket.WebSocketTunnel):
    """Kubernetes Tunnel"""

    def __init__(self, tunnel, url, address):
        kubeconfig = url.params.get("kubeconfig")
        client_cert = url.params.get("client_cert")
        client_key = url.params.get("client_key")
        ca_cert = url.params.get("ca_cert")
        if not kubeconfig and (not client_cert or not client_key or not ca_cert):
            raise utils.ParamError(
                "Parameter `kubeconfig` or (`client_cert` and `client_key` and `ca_cert`) must be specified"
            )
        elif kubeconfig:
            if not os.path.isfile(kubeconfig):
                raise ValueError("Kube config file %s not exist" % kubeconfig)
            raise NotImplementedError(kubeconfig)
        else:
            if not os.path.isfile(ca_cert):
                raise ValueError("CA cert file %s not exist" % ca_cert)
            if not os.path.isfile(client_cert):
                raise ValueError("Client cert file %s not exist" % client_cert)
            if not os.path.isfile(client_key):
                raise ValueError("Client key file %s not exist" % client_key)
        self._namespace = url.params.get("namespace", "default")
        self._pod = url.params.get("pod")
        if not self._pod:
            raise utils.ParamError("Parameter `pod` must be specified")
        container = url.params.get("container", "")
        commands = [url.path, address[0], address[1]]
        ws_url = "wss://%s:%d/api/v1/namespaces/%s/pods/%s/exec?container=%s&stdin=1&stdout=1&stderr=1&tty=0&%s" % (
            url.host,
            url.port,
            self._namespace,
            self._pod,
            container,
            "&".join([("command=%s" % it) for it in commands]),
        )
        # If `tty` is true, `stderr` MUST be false. Multiplexing is not supported
        # in this case. The output of stdout and stderr will be combined to a
        # single stream.
        ws_url += "&ca_cert=%s&client_cert=%s&client_key=%s" % (
            ca_cert,
            client_cert,
            client_key,
        )
        super(KubernetesTunnel, self).__init__(tunnel, utils.Url(ws_url), address)

    async def _wait_for_connecting(self, timeout=15):
        time0 = time.time()
        while time.time() - time0 < timeout:
            _, stderr = await self.read_output()
            if b"OCI runtime exec failed" in stderr:
                utils.logger.error(
                    "[%s] Exec command in pod %s:%s failed: %s"
                    % (
                        self.__class__.__name__,
                        self._namespace,
                        self._pod,
                        stderr.decode(),
                    )
                )
                return False
            elif stderr.startswith(b"[OKAY]"):
                return True
            elif stderr.startswith(b"[FAIL]"):
                utils.logger.warn(
                    "[%s] Connect %s:%d failed: %s"
                    % (
                        self.__class__.__name__,
                        self._addr,
                        self._port,
                        stderr.decode(),
                    )
                )
                return False
        else:
            utils.logger.warning(
                "[%s] Wait for connecting timeout" % self.__class__.__name__
            )
            return False

    async def connect(self):
        if not await super(KubernetesTunnel, self).connect():
            return False
        return await self._wait_for_connecting()

    async def write(self, buffer):
        return await super(KubernetesTunnel, self).write(b"\x00" + buffer)

    async def read_output(self):
        while True:
            buffer = await super(KubernetesTunnel, self).read()
            if len(buffer) <= 1:
                continue
            if buffer[0] == 1:
                return buffer[1:], b""
            elif buffer[0] == 2:
                return b"", buffer[1:]
            else:
                raise NotImplementedError("Unsupported output: %r" % buffer)

    async def read(self):
        while True:
            stdout, stderr = await self.read_output()
            return stdout


registry.tunnel_registry.register("k8s", KubernetesTunnel)
