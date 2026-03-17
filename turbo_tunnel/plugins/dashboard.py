# -*- coding: utf-8 -*-
"""Dashboard Plugin - Web-based monitoring dashboard"""

import asyncio
import json
import os
import time
from collections import deque
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import tornado.web
import tornado.websocket
import tornado.httpserver

from . import Plugin
from .. import registry, utils
from .. import server


class DashboardConnection:
    """Dashboard connection data model"""

    def __init__(
        self,
        client_address: Tuple[str, int],
        target_address: Tuple[str, int],
        tunnel_address: Optional[Tuple[str, int]] = None,
    ) -> None:
        self._client_address: Tuple[str, int] = client_address
        self._target_address: Tuple[str, int] = target_address
        self._tunnel_address: Optional[Tuple[str, int]] = tunnel_address
        self._start_time: float = time.time()
        self._end_time: Optional[float] = None
        self._bytes_sent: int = 0
        self._bytes_received: int = 0

        # Rate calculation using sliding window (5 seconds)
        self._send_history: "deque[Tuple[int, float]]" = deque(maxlen=5)
        self._recv_history: "deque[Tuple[int, float]]" = deque(maxlen=5)
        self._last_update: float = time.time()

    @property
    def client_address(self) -> Tuple[str, int]:
        """Get client address"""
        return self._client_address

    @property
    def target_address(self) -> Tuple[str, int]:
        """Get target address"""
        return self._target_address

    @property
    def tunnel_address(self) -> Optional[Tuple[str, int]]:
        """Get tunnel address"""
        return self._tunnel_address

    @tunnel_address.setter
    def tunnel_address(self, value: Optional[Tuple[str, int]]) -> None:
        """Set tunnel address"""
        self._tunnel_address = value

    @property
    def start_time(self) -> float:
        """Get start time"""
        return self._start_time

    @property
    def end_time(self) -> Optional[float]:
        """Get end time"""
        return self._end_time

    @property
    def connection_id(self) -> str:
        """Generate unique connection ID"""
        return f"{self._client_address[0]}:{self._client_address[1]}->{self._target_address[0]}:{self._target_address[1]}"

    @property
    def is_active(self) -> bool:
        """Check if connection is active"""
        return self._end_time is None

    @property
    def duration(self) -> str:
        """Get duration string in HH:MM:SS format"""
        end = self._end_time or time.time()
        delta = int(end - self._start_time)
        hours = delta // 3600
        minutes = (delta % 3600) // 60
        seconds = delta % 60
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    @property
    def start_time_str(self) -> str:
        """Get formatted start time"""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._start_time))

    @property
    def bytes_sent(self) -> int:
        """Get total bytes sent"""
        return self._bytes_sent

    @property
    def bytes_received(self) -> int:
        """Get total bytes received"""
        return self._bytes_received

    @property
    def send_rate(self) -> float:
        """Calculate send rate in bytes/sec"""
        try:
            if not self._send_history:
                return 0.0
            total_bytes = sum(b for b, _ in self._send_history)
            if len(self._send_history) < 2:
                return float(total_bytes) if total_bytes >= 0 else 0.0
            time_span = self._send_history[-1][1] - self._send_history[0][1]
            if time_span <= 0:
                return 0.0
            rate = total_bytes / time_span
            # Ensure the rate is a valid finite number
            return rate if (rate >= 0 and rate < float("inf")) else 0.0
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0

    @property
    def recv_rate(self) -> float:
        """Calculate receive rate in bytes/sec"""
        try:
            if not self._recv_history:
                return 0.0
            total_bytes = sum(b for b, _ in self._recv_history)
            if len(self._recv_history) < 2:
                return float(total_bytes) if total_bytes >= 0 else 0.0
            time_span = self._recv_history[-1][1] - self._recv_history[0][1]
            if time_span <= 0:
                return 0.0
            rate = total_bytes / time_span
            # Ensure the rate is a valid finite number
            return rate if (rate >= 0 and rate < float("inf")) else 0.0
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0

    def update_sent_bytes(self, size: int) -> None:
        """Update sent bytes and calculate rate"""
        self._bytes_sent += size
        current_time = time.time()
        self._send_history.append((size, current_time))
        self._last_update = current_time

    def update_recv_bytes(self, size: int) -> None:
        """Update received bytes and calculate rate"""
        self._bytes_received += size
        current_time = time.time()
        self._recv_history.append((size, current_time))
        self._last_update = current_time

    def close(self) -> None:
        """Mark connection as closed"""
        self._end_time = time.time()

    def to_dict(self) -> Dict[str, Any]:  # pyright: ignore[reportExplicitAny]
        """Serialize to dictionary for JSON transmission"""
        # Format tunnel address
        tunnel_str = "--"
        if self._tunnel_address:
            if (
                isinstance(self._tunnel_address, (tuple, list))
                and len(self._tunnel_address) >= 2
            ):
                host = self._tunnel_address[0]
                port = self._tunnel_address[1]
                if host and port:
                    tunnel_str = f"{host}:{port}"
                elif host:
                    tunnel_str = host

        # Get rates and ensure they are valid numbers
        send_rate = self.send_rate
        recv_rate = self.recv_rate

        # Additional safety check for JSON serialization
        if (
            not isinstance(send_rate, (int, float))
            or send_rate < 0
            or send_rate == float("inf")
        ):
            send_rate = 0.0
        if (
            not isinstance(recv_rate, (int, float))
            or recv_rate < 0
            or recv_rate == float("inf")
        ):
            recv_rate = 0.0

        return {
            "id": self.connection_id,
            "source": f"{self._client_address[0]}:{self._client_address[1]}",
            "target": f"{self._target_address[0]}:{self._target_address[1]}",
            "tunnel": tunnel_str,
            "start_time": self.start_time_str,
            "duration": self.duration,
            "bytes_sent": self.bytes_sent,
            "bytes_recv": self.bytes_received,
            "send_rate": round(send_rate, 2),
            "recv_rate": round(recv_rate, 2),
            "is_active": self.is_active,
        }


class DashboardWebSocketHandler(tornado.websocket.WebSocketHandler):
    """WebSocket handler for real-time data push"""

    clients: Set["DashboardWebSocketHandler"] = set()

    def check_origin(
        self, origin: str
    ) -> bool:  # pyright: ignore[reportImplicitOverride]
        """Allow all origins for WebSocket connections"""
        return True

    def open(
        self, *args: Any, **kwargs: Any
    ) -> None:  # pyright: ignore[reportImplicitOverride]
        """WebSocket connection opened"""
        self.__class__.clients.add(self)
        utils.logger.info(
            "[%s] Client connected from %s"
            % (self.__class__.__name__, self.request.remote_ip)
        )

    def on_close(self) -> None:  # pyright: ignore[reportImplicitOverride]
        """WebSocket connection closed"""
        self.__class__.clients.discard(self)
        utils.logger.info(
            "[%s] Client disconnected from %s"
            % (self.__class__.__name__, self.request.remote_ip)
        )

    def on_message(
        self, message: Union[str, bytes]
    ) -> None:  # pyright: ignore[reportImplicitOverride]
        """Receive message from client (reserved for future commands)"""
        try:
            if isinstance(message, bytes):
                message = message.decode("utf-8")
            data = json.loads(message)
            utils.logger.debug(
                "[%s] Received message: %s" % (self.__class__.__name__, data)
            )
        except json.JSONDecodeError:
            utils.logger.warning(
                "[%s] Invalid JSON message: %s" % (self.__class__.__name__, message)
            )

    @classmethod
    def broadcast(cls, message: Dict[str, Any]) -> None:
        """Broadcast message to all connected clients"""
        json_data = json.dumps(message)
        dead_clients: Set["DashboardWebSocketHandler"] = set()

        for client in cls.clients:
            try:
                client.write_message(json_data)
            except Exception as e:
                utils.logger.error(
                    "[%s] Failed to send message to client: %s" % (cls.__name__, e)
                )
                dead_clients.add(client)

        # Remove dead clients
        for client in dead_clients:
            cls.clients.discard(client)


class StaticFileHandler(tornado.web.StaticFileHandler):
    """Static file handler for dashboard HTML"""

    def set_default_headers(self) -> None:  # pyright: ignore[reportImplicitOverride]
        """Set headers to prevent caching during development"""
        self.set_header(
            "Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"
        )


class DashboardPlugin(Plugin):
    """Dashboard plugin for web-based monitoring"""

    def __init__(self, host: str = "0.0.0.0", port: int = 8888) -> None:
        """Initialize dashboard plugin

        Args:
            host: Listen host address
            port: Listen port
        """
        self._host: str = host
        self._port: int = port
        self._http_server: Optional[tornado.httpserver.HTTPServer] = None
        self._connections: Dict[str, DashboardConnection] = {}
        self._running: bool = False
        self._push_task: "Optional[asyncio.Task[None]]" = None

        # Statistics
        self._total_connections: int = 0
        self._total_bytes_sent: int = 0
        self._total_bytes_recv: int = 0

        # Auto cleanup configuration
        self._cleanup_interval: int = 300  # 5 minutes
        self._max_connections: int = 100  # Keep at most 100 connections

    def on_load(self) -> None:  # pyright: ignore[reportImplicitOverride]
        """Plugin loaded, start HTTP server"""
        try:
            # Get static file path
            plugin_dir = os.path.dirname(os.path.abspath(__file__))
            static_path = os.path.join(plugin_dir, "static")

            # Ensure static directory exists
            if not os.path.exists(static_path):
                os.makedirs(static_path)
                utils.logger.info(
                    "[%s] Created static directory: %s"
                    % (self.__class__.__name__, static_path)
                )

            # Create Tornado application
            app = tornado.web.Application(
                [
                    (r"/ws", DashboardWebSocketHandler),
                    (
                        r"/(.*)",
                        StaticFileHandler,
                        {"path": static_path, "default_filename": "dashboard.html"},
                    ),
                ]
            )

            # Start HTTP server
            self._http_server = tornado.httpserver.HTTPServer(app)
            if self._http_server:
                self._http_server.listen(self._port, self._host)

            self._running = True

            # Start data push task
            self._push_task = asyncio.ensure_future(self._push_data_task())

            utils.logger.info(
                "[%s] Dashboard server started at http://%s:%d"
                % (
                    self.__class__.__name__,
                    self._host if self._host != "0.0.0.0" else "localhost",
                    self._port,
                )
            )

        except Exception as e:
            utils.logger.error(
                "[%s] Failed to start dashboard: %s" % (self.__class__.__name__, e)
            )
            raise

    def on_unload(self) -> None:  # pyright: ignore[reportImplicitOverride]
        """Plugin unloaded, stop HTTP server"""
        self._running = False

        if self._push_task:
            self._push_task.cancel()

        if self._http_server:
            self._http_server.stop()

        utils.logger.info("[%s] Dashboard stopped" % self.__class__.__name__)

    def on_new_connection(
        self, connection: server.TunnelConnection
    ) -> None:  # pyright: ignore[reportImplicitOverride]
        """Handle new connection event"""
        conn = DashboardConnection(
            connection.client_address,
            connection.target_address,
            connection.tunnel_address,
        )

        self._connections[conn.connection_id] = conn
        self._total_connections += 1

        utils.logger.debug(
            "[%s] New connection tracked: %s, tunnel_address: %s"
            % (self.__class__.__name__, conn.connection_id, connection.tunnel_address)
        )

    def on_tunnel_address_updated(
        self, connection: server.TunnelConnection, tunnel_address: Tuple[str, int]
    ) -> None:  # pyright: ignore[reportImplicitOverride]
        """Handle tunnel address update event"""
        conn_id = self._get_connection_id(connection)
        if conn_id in self._connections:
            self._connections[conn_id].tunnel_address = tunnel_address
            utils.logger.debug(
                "[%s] Tunnel address updated for %s: %s"
                % (self.__class__.__name__, conn_id, tunnel_address)
            )

    def on_data_sent(
        self, connection: server.TunnelConnection, buffer: bytes
    ) -> None:  # pyright: ignore[reportImplicitOverride]
        """Handle data sent event"""
        conn_id = self._get_connection_id(connection)
        if conn_id in self._connections:
            size = len(buffer)
            self._connections[conn_id].update_sent_bytes(size)
            self._total_bytes_sent += size

    def on_data_recevied(
        self, connection: server.TunnelConnection, buffer: bytes
    ) -> None:  # pyright: ignore[reportImplicitOverride]
        """Handle data received event"""
        conn_id = self._get_connection_id(connection)
        if conn_id in self._connections:
            size = len(buffer)
            self._connections[conn_id].update_recv_bytes(size)
            self._total_bytes_recv += size

    def on_connection_closed(
        self, connection: server.TunnelConnection
    ) -> None:  # pyright: ignore[reportImplicitOverride]
        """Handle connection closed event"""
        conn_id = self._get_connection_id(connection)
        if conn_id in self._connections:
            self._connections[conn_id].close()
            utils.logger.debug(
                "[%s] Connection closed: %s" % (self.__class__.__name__, conn_id)
            )

    def _get_connection_id(self, connection: server.TunnelConnection) -> str:
        """Generate connection ID from TunnelConnection"""
        return f"{connection.client_address[0]}:{connection.client_address[1]}->{connection.target_address[0]}:{connection.target_address[1]}"

    def _cleanup_old_connections(self) -> None:
        """Clean up old closed connections"""
        current_time = time.time()
        to_remove: List[str] = []

        for conn_id, conn in self._connections.items():
            if not conn.is_active and conn.end_time:
                if current_time - conn.end_time > self._cleanup_interval:
                    to_remove.append(conn_id)

        for conn_id in to_remove:
            del self._connections[conn_id]

        if to_remove:
            utils.logger.debug(
                "[%s] Cleaned up %d old connections"
                % (self.__class__.__name__, len(to_remove))
            )

        # Limit total connections count
        if len(self._connections) > self._max_connections:
            # Keep only the most recent connections
            sorted_conns = sorted(
                self._connections.items(), key=lambda x: x[1].start_time, reverse=True
            )
            self._connections = dict(sorted_conns[: self._max_connections])
            utils.logger.debug(
                "[%s] Limited connections to %d"
                % (self.__class__.__name__, self._max_connections)
            )

    def _aggregate_data(self) -> Dict[str, Any]:
        """Aggregate connection data for broadcasting"""
        connections_data: List[Dict[str, Any]] = []
        active_count = 0

        for conn in self._connections.values():
            connections_data.append(conn.to_dict())
            if conn.is_active:
                active_count += 1

        # Sort by start time (newest first)
        connections_data.sort(key=lambda x: x["start_time"], reverse=True)

        return {
            "type": "update",
            "timestamp": int(time.time()),
            "connections": connections_data,
            "statistics": {
                "total_connections": self._total_connections,
                "active_connections": active_count,
                "total_bytes_sent": self._total_bytes_sent,
                "total_bytes_recv": self._total_bytes_recv,
            },
        }

    async def _push_data_task(self) -> None:
        """Periodic task to push data to clients"""
        while self._running:
            try:
                # Cleanup old connections
                self._cleanup_old_connections()

                # Aggregate and broadcast data
                if DashboardWebSocketHandler.clients:
                    data = self._aggregate_data()
                    DashboardWebSocketHandler.broadcast(data)

                await asyncio.sleep(1)  # Push every second

            except asyncio.CancelledError:
                break
            except Exception as e:
                utils.logger.error(
                    "[%s] Error in push task: %s" % (self.__class__.__name__, e)
                )
                await asyncio.sleep(1)
