"""A TCP proxy with independently controlled directions (spec Section 14.3).

Blackholing traffic alone establishes nothing: it neither selects a branch nor
makes the runner exit. This proxy exists so a harness can

* drop the client-to-server direction, so a request is genuinely never
  delivered, and then terminate the transport;
* withhold the server-to-client direction, so a commit that really reached the
  server has its acknowledgement suppressed, and then terminate the transport;
* keep the server-side socket open after the client process dies, so a real
  server session outlives a dead client.

Synchronisation with the runner happens at the engine's own commit boundaries
through ``migr8.testing.hooks``; this module only controls the bytes.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time

LOGGER = logging.getLogger("tests.proxy")

_CHUNK = 65536


class _Pair:
    """One accepted client connection and its matching upstream connection."""

    __slots__ = ("client", "server", "proxy", "threads", "closed")

    def __init__(self, client: socket.socket, server: socket.socket, proxy: DirectionalProxy):
        self.client = client
        self.server = server
        self.proxy = proxy
        self.threads: list[threading.Thread] = []
        self.closed = False

    def start(self) -> None:
        for name, source, sink, direction in (
            ("c2s", self.client, self.server, "to_server"),
            ("s2c", self.server, self.client, "to_client"),
        ):
            thread = threading.Thread(
                target=self._pump, args=(source, sink, direction), name=f"proxy-{name}",
                daemon=True,
            )
            thread.start()
            self.threads.append(thread)

    def _pump(self, source: socket.socket, sink: socket.socket, direction: str) -> None:
        forward_attr = f"forward_{direction}"
        pending = b""
        while not self.closed and not self.proxy.stopping:
            try:
                source.settimeout(0.05)
                try:
                    chunk = source.recv(_CHUNK)
                except socket.timeout:
                    chunk = b""
                except OSError:
                    break
                if chunk == b"" and not self._peer_alive(source):
                    break
                pending += chunk
                if not getattr(self.proxy, forward_attr):
                    # Held, not forwarded.  Bytes stay buffered here; if the
                    # direction is never reopened they are never delivered.
                    if self.proxy.discard_held and pending:
                        self.proxy.held_bytes[direction] += len(pending)
                        pending = b""
                    continue
                if pending:
                    try:
                        sink.sendall(pending)
                    except OSError:
                        break
                    pending = b""
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.debug("proxy pump %s stopped: %s", direction, exc)
                break
        self._finish(direction)

    @staticmethod
    def _peer_alive(source: socket.socket) -> bool:
        try:
            return source.fileno() != -1
        except OSError:
            return False

    def _finish(self, direction: str) -> None:
        if direction == "to_server" and self.proxy.keep_server_open_on_client_close:
            # The client is gone but the upstream session is deliberately left
            # connected, so a real server session outlives the dead client.
            return
        self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for sock in (self.client, self.server):
            try:
                sock.close()
            except OSError:
                pass

    def reset_client(self) -> None:
        """Abruptly close the client-facing socket so the client sees a hard error."""
        try:
            self.client.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
        except OSError:
            pass
        try:
            self.client.close()
        except OSError:
            pass


class DirectionalProxy:
    """Forwards 127.0.0.1:<assigned> to ``target_host:target_port``."""

    def __init__(self, target_host: str, target_port: int) -> None:
        self.target = (target_host, int(target_port))
        self.forward_to_server = True
        self.forward_to_client = True
        self.keep_server_open_on_client_close = False
        self.discard_held = False
        self.held_bytes = {"to_server": 0, "to_client": 0}
        self.stopping = False
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._pairs: list[_Pair] = []
        self._lock = threading.Lock()
        self.host = "127.0.0.1"
        self.port = 0

    # --- lifecycle -------------------------------------------------------------

    def start(self) -> tuple[str, int]:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, 0))
        listener.listen(16)
        listener.settimeout(0.2)
        self._listener = listener
        self.port = listener.getsockname()[1]
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="proxy-accept", daemon=True
        )
        self._accept_thread.start()
        return self.host, self.port

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self.stopping:
            try:
                client, _address = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                server = socket.create_connection(self.target, timeout=20)
            except OSError as exc:
                LOGGER.warning("proxy cannot reach %s: %s", self.target, exc)
                client.close()
                continue
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            server.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            pair = _Pair(client, server, self)
            with self._lock:
                self._pairs.append(pair)
            pair.start()

    def stop(self) -> None:
        self.stopping = True
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        with self._lock:
            pairs = list(self._pairs)
            self._pairs.clear()
        for pair in pairs:
            pair.close()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2)

    def __enter__(self) -> DirectionalProxy:
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    # --- controls ----------------------------------------------------------------

    @property
    def connection_count(self) -> int:
        with self._lock:
            return len([pair for pair in self._pairs if not pair.closed])

    def wait_for_connection(self, *, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while self.connection_count == 0:
            if time.monotonic() > deadline:
                raise AssertionError("no client connected to the proxy")
            time.sleep(0.02)

    def hold_requests(self) -> None:
        """Stop delivering client-to-server bytes."""
        self.forward_to_server = False

    def drop_requests(self) -> None:
        """Stop delivering client-to-server bytes and discard what arrives."""
        self.discard_held = True
        self.forward_to_server = False

    def hold_responses(self) -> None:
        """Let requests through but withhold server-to-client bytes."""
        self.forward_to_client = False

    def resume(self) -> None:
        self.forward_to_server = True
        self.forward_to_client = True

    def reset_clients(self) -> None:
        """Terminate the client side of every connection with a hard error."""
        with self._lock:
            pairs = list(self._pairs)
        for pair in pairs:
            pair.reset_client()

    def orphan_server_sessions(self) -> None:
        """Keep upstream sockets open even after the client disappears."""
        self.keep_server_open_on_client_close = True

    def close_server_sessions(self) -> None:
        self.keep_server_open_on_client_close = False
        with self._lock:
            pairs = list(self._pairs)
            self._pairs.clear()
        for pair in pairs:
            pair.close()
