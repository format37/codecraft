"""The seats of a game, as the official engine 1.3.0 runs them (fixtures `crash-*`, `actions-*`; the probes of step 6).

- `TcpPlayer`: one of the 15 official clients, or anything that speaks their protocol (trans.py). The engine listens
  on the seat's `host` and `port` ("Waiting for connection on port N"; every seat at once, before any is accepted),
  waits up to `accept_timeout` seconds for the client ("Got connection on port N", or "Timeout accepting player on
  port N" and "Stop listening port N"), reads its token, then every tick sends `GetAction(view, debug_available =
  false)` and reads the answer, each read within `timeout` seconds (a socket read timeout, as the official engine's
  SO_RCVTIMEO; None = no limit). A `DebugMessage` before the answer is read and ignored; `DebugUpdateDone` and
  `RequestDebugState` crash the seat, as do an unknown message, a late answer and a closed connection.
- `EmptyPlayer`: never answers; its entities keep their stored orders.
- `QuickStartPlayer`: the built-in bot (quickstart.py), asked in-process like a client.

A seat that fails is crashed for the rest of the game: it gets no further message, its connection is closed at once
("Dropping tcp player on port N"; none when the token was wrong: no player was made), and its entities keep their
stored orders. `PlayerError` carries the official error text: the log says "Player error: " + it, results.json
"Player crashed: " + it.
"""
from __future__ import annotations

import os
import socket
import threading

from . import quickstart
from .model import EntityAction, PlayerView
from .trans import (ACTION_MESSAGE, DEBUG_MESSAGE, DEBUG_UPDATE_DONE, REQUEST_DEBUG_STATE, ProtocolError,
                    StreamReader, Writer, read_action, write_finish, write_get_action)

TIMEOUT = "IO error: Resource temporarily unavailable (os error 11)"
CLOSED = "IO error: failed to fill whole buffer"
NO_CONNECTION = "IO error: IO error: connection reset"
TOKEN_MISMATCH = "IO error: IO error: Token mismatch"
DEBUG_UPDATE_DONE_ERROR = "IO error: Unexpected debug update done message in get_action"
DEBUG_STATE_ERROR = "IO error: Requested debug state with no debug interface available"


class PlayerError(Exception):
    """A seat failed; the text is the official engine's for the same failure."""


def io_error(exc: BaseException) -> PlayerError:
    """The official text for a failure of the connection."""
    if isinstance(exc, PlayerError):
        return exc
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return PlayerError(TIMEOUT)
    if isinstance(exc, EOFError):
        return PlayerError(CLOSED)
    if isinstance(exc, OSError) and exc.errno:
        return PlayerError(f"IO error: {os.strerror(exc.errno)} (os error {exc.errno})")
    return PlayerError(f"IO error: {exc}")


class Log:
    """The engine's log on stderr: "INFO - message", as the official engine prints it without a terminal."""
    LEVELS = {"off": 0, "error": 1, "warn": 2, "info": 3, "debug": 4, "trace": 5}

    def __init__(self, level: str = "info", stream=None) -> None:
        import sys
        self.level = self.LEVELS.get(str(level).lower(), 3)
        self.stream = stream if stream is not None else sys.stderr
        self.lock = threading.Lock()

    def _write(self, level: int, name: str, msg: str) -> None:
        if level <= self.level:
            with self.lock:
                self.stream.write(f"{name} - {msg}\n")
                self.stream.flush()

    def error(self, msg: str) -> None:
        self._write(1, "ERROR", msg)

    def warn(self, msg: str) -> None:
        self._write(2, "WARN", msg)

    def info(self, msg: str) -> None:
        self._write(3, "INFO", msg)


class Seat:
    """What the engine needs of a player."""
    crashed = False
    comment: str | None = None

    def start(self) -> None:
        """Before the game: connect (a TCP seat accepts its client here)."""

    def get_action(self, view: PlayerView) -> dict[int, EntityAction]:
        """The orders for this tick, in the seat's frame; raises PlayerError when the seat fails."""
        return {}

    def finish(self) -> None:
        """After the last tick."""

    def crash(self, error: str, log: Log) -> None:
        self.crashed = True
        self.comment = f"Player crashed: {error}"
        log.warn(f"Player error: {error}")


class EmptyPlayer(Seat):
    pass


class QuickStartPlayer(Seat):
    def get_action(self, view: PlayerView) -> dict[int, EntityAction]:
        return quickstart.get_action(view)


class TcpPlayer(Seat):
    def __init__(self, host: str | None, port: int, accept_timeout: float | None, timeout: float | None,
                 token: str | None, log: Log) -> None:
        # as the official runner: a null host listens on 127.0.0.1, a null token takes any token
        self.host, self.port = "127.0.0.1" if host is None else host, int(port)
        self.accept_timeout = None if accept_timeout is None else float(accept_timeout)
        self.timeout = None if timeout is None else float(timeout)
        self.token = token
        self.log = log
        self.listener: socket.socket | None = None
        self.sock: socket.socket | None = None
        self.f = None
        self.reader: StreamReader | None = None
        self.failure: str | None = None     # a failure while connecting, reported when the game starts

    def listen(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(1)
        self.listener = s
        self.log.info(f"Waiting for connection on port {self.port}")

    def start(self) -> None:
        """Accept the client and check its token; a failure is kept in `failure` (accepting runs in a thread)."""
        try:
            self.listener.settimeout(self.accept_timeout)
            try:
                conn, _ = self.listener.accept()
            except (socket.timeout, TimeoutError):
                self.log.info(f"Timeout accepting player on port {self.port}")
                self.listener.close()
                self.log.info(f"Stop listening port {self.port}")
                raise PlayerError(NO_CONNECTION) from None
            self.listener.close()
            self.log.info(f"Got connection on port {self.port}")
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.settimeout(self.accept_timeout)
                f = conn.makefile("rwb")
                token = StreamReader(f).string()
            except (OSError, EOFError, ProtocolError, UnicodeDecodeError):
                conn.close()
                raise PlayerError(NO_CONNECTION) from None
            if self.token is not None and token != self.token:
                conn.close()
                raise PlayerError(TOKEN_MISMATCH)
            conn.settimeout(self.timeout)
            self.sock, self.f, self.reader = conn, f, StreamReader(f)
        except PlayerError as exc:
            self.failure = str(exc)

    def get_action(self, view: PlayerView) -> dict[int, EntityAction]:
        w = Writer()
        write_get_action(w, view, False)
        try:
            self.f.write(w.buf)
            self.f.flush()
            r = self.reader
            while True:
                tag = r.i32()
                if tag == ACTION_MESSAGE:
                    return read_action(r)
                if tag == DEBUG_MESSAGE:
                    skip_debug_command(r)
                elif tag == DEBUG_UPDATE_DONE:
                    raise PlayerError(DEBUG_UPDATE_DONE_ERROR)
                elif tag == REQUEST_DEBUG_STATE:
                    raise PlayerError(DEBUG_STATE_ERROR)
                else:
                    raise PlayerError(f"IO error: Unexpected tag {tag}")
        except (OSError, EOFError, ProtocolError, UnicodeDecodeError, ValueError) as exc:
            raise io_error(exc) from None

    def crash(self, error: str, log: Log) -> None:
        super().crash(error, log)
        self.drop()

    def finish(self) -> None:
        if self.sock is None:
            return
        try:
            w = Writer()
            write_finish(w)
            self.f.write(w.buf)
            self.f.flush()
        except (OSError, ValueError):
            pass
        self.drop()

    def drop(self) -> None:
        if self.sock is None:
            return
        for c in (self.f, self.sock):
            try:
                c.close()
            except OSError:
                pass
        self.sock = self.f = self.reader = None
        self.log.info(f"Dropping tcp player on port {self.port}")


# A DebugMessage carries a DebugCommand, read and thrown away (clients/Python/model/debug_*.py) -----------------
def skip_debug_command(r) -> None:
    tag = r.i32()
    if tag == 0:                    # Add(DebugData)
        _skip_debug_data(r)
    elif tag == 2:                  # SetAutoFlush(enable)
        r.bool()
    elif tag not in (1, 3):         # Clear, Flush
        raise PlayerError(f"IO error: Unexpected tag {tag}")


def _skip_debug_data(r) -> None:
    tag = r.i32()
    if tag == 0:                    # Log(text)
        r.string()
    elif tag == 1:                  # Primitives(vertices, primitive_type)
        for _ in range(r.count()):
            _skip_vertex(r)
        r.i32()
    elif tag == 2:                  # PlacedText(vertex, text, alignment, size)
        _skip_vertex(r)
        r.string()
        r.f32()
        r.f32()
    else:
        raise PlayerError(f"IO error: Unexpected tag {tag}")


def _skip_vertex(r) -> None:        # ColoredVertex(world_pos?, screen_offset, color)
    if r.bool():
        r.f32()
        r.f32()
    for _ in range(6):
        r.f32()
