"""A scripted TCP player for the oracle: it speaks the clients' protocol, plays a fixed script and records every
PlayerView it is sent (the fog-of-war oracle), and it can misbehave on cue (the crash oracle).

    p = Puppet({0: {4: move((10, 1))}, 3: {4: move((1, 10))}})     # view tick -> the Action to answer with
    p = Puppet(lambda view: {...})                                  # or a function of the view
    p = Puppet(crash_at=3)        # close the connection instead of answering at tick 3
    p = Puppet(sleep_at={2: 1.5}) # answer tick 2 after 1.5 s
    p = Puppet(garbage_at=1)      # answer tick 1 with an unknown message tag
    p = Puppet(debug_at={1: 0})   # send a debug message (ClientMessage tag 0, 2 or 3) before the answer to tick 1
    p = Puppet(token="wrong")     # send this token instead of the configured one

`loadfrom.run` gives it a port and a token and starts it once the engine listens.
"""
from __future__ import annotations

import socket
import threading
import time
from typing import Callable

from codecraft_sim.model import EntityAction, PlayerView, action_from_json, action_json
from codecraft_sim.trans import StreamReader, Writer, read_server_message, write_action_message

Script = dict[int, dict[int, EntityAction]] | Callable[[PlayerView], dict[int, EntityAction]]


class Puppet:
    def __init__(self, script: Script | None = None, *, crash_at: int | None = None,
                 sleep_at: dict[int, float] | None = None, garbage_at: int | None = None,
                 debug_at: dict[int, int] | None = None, token: str | None = None, connect: bool = True) -> None:
        self.script = script if script is not None else {}
        self.crash_at = crash_at
        self.sleep_at = dict(sleep_at or {})
        self.garbage_at = garbage_at
        self.debug_at = dict(debug_at or {})    # view tick -> ClientMessage tag sent first: 0 = DebugMessage(Clear),
        #                                         2 = DebugUpdateDone, 3 = RequestDebugState
        self.token = token
        self.connect = connect                  # False: never connect (the accept-timeout case)
        self.views: list[PlayerView] = []       # every GetAction's view, in order
        self.sent: dict[int, dict[int, EntityAction]] = {}  # view tick -> the actions answered
        self.finished = False                   # the server sent Finish
        self.closed_by_server = False           # the server closed the connection before Finish
        self.error: str | None = None           # anything else that went wrong on our side
        self.thread: threading.Thread | None = None

    def spec(self) -> dict:
        """The puppet as fixture JSON: {"puppet": {"script": {tick: {entity id: action}}, ...misbehaviour}}."""
        if callable(self.script):
            raise ValueError("a puppet with a function as its script cannot be stored in a fixture")
        out: dict = {"script": {str(t): {str(e): action_json(a) for e, a in acts.items()}
                                for t, acts in sorted(self.script.items())}}
        for key in ("crash_at", "garbage_at", "token"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        if self.sleep_at:
            out["sleep_at"] = {str(t): s for t, s in sorted(self.sleep_at.items())}
        if self.debug_at:
            out["debug_at"] = {str(t): tag for t, tag in sorted(self.debug_at.items())}
        if not self.connect:
            out["connect"] = False
        return {"puppet": out}

    @classmethod
    def from_spec(cls, spec: dict) -> "Puppet":
        d = spec["puppet"]
        script = {int(t): {int(e): action_from_json(a) for e, a in acts.items()}
                  for t, acts in d.get("script", {}).items()}
        return cls(script, crash_at=d.get("crash_at"), garbage_at=d.get("garbage_at"), token=d.get("token"),
                   sleep_at={int(t): s for t, s in d.get("sleep_at", {}).items()},
                   debug_at={int(t): tag for t, tag in d.get("debug_at", {}).items()}, connect=d.get("connect", True))

    def actions_for(self, view: PlayerView) -> dict[int, EntityAction]:
        if callable(self.script):
            return self.script(view)
        return self.script.get(view.current_tick, {})

    def start(self, host: str, port: int, token: str) -> None:
        self.thread = threading.Thread(target=self._run, args=(host, port, token), daemon=True,
                                       name=f"puppet-{port}")
        self.thread.start()

    def join(self, timeout: float) -> None:
        if self.thread is not None:
            self.thread.join(timeout)

    def _run(self, host: str, port: int, token: str) -> None:
        if not self.connect:
            return
        sock = None
        try:
            sock = socket.create_connection((host, port), timeout=30)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            f = sock.makefile("rwb")
            w = Writer()
            w.string(self.token if self.token is not None else token)
            f.write(w.buf)
            f.flush()
            r = StreamReader(f)
            while True:
                msg = read_server_message(r)
                if msg[0] == "finish":
                    self.finished = True
                    return
                if msg[0] != "get_action":
                    raise RuntimeError(f"unexpected server message {msg[0]}")
                view = msg[1]
                self.views.append(view)
                tick = view.current_tick
                if self.crash_at is not None and tick >= self.crash_at:
                    sock.shutdown(socket.SHUT_RDWR)
                    return
                if tick in self.sleep_at:
                    time.sleep(self.sleep_at[tick])
                w = Writer()
                if tick in self.debug_at:
                    w.i32(self.debug_at[tick])
                    if self.debug_at[tick] == 0:
                        w.i32(1)        # DebugCommand::Clear
                if self.garbage_at == tick:
                    w.i32(77)           # no such ClientMessage tag
                else:
                    actions = self.actions_for(view)
                    self.sent[tick] = actions
                    write_action_message(w, actions)
                f.write(w.buf)
                f.flush()
        except (EOFError, ConnectionError):
            self.closed_by_server = True
        except Exception as exc:  # noqa: BLE001 - recorded for the caller to look at
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
