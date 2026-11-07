"""The GUI's connection to the privileged helper, plus read-only data it can load itself."""

from __future__ import annotations

import json
import os
import sys

from gi.repository import Gio, GLib

from .. import config, overview, paths, status


class HelperClient:
    """Talks JSON lines to `pkexec lifeboat helper`, started on first use.

    Calls are asynchronous: callback(result, error) runs on the GTK main loop.
    """

    def __init__(self):
        self.proc: Gio.Subprocess | None = None
        self.stdin = None
        self.reader: Gio.DataInputStream | None = None
        self.pending: dict[int, callable] = {}
        self.queue: list[str] = []
        self.next_id = 1
        self.ready = False
        self.on_state_change = None  # called with True/False when unlocked/locked

    @property
    def unlocked(self) -> bool:
        return self.ready

    def _command(self) -> tuple[list[str], dict]:
        env = {}
        if os.environ.get("LIFEBOAT_DEV"):
            # Running from a source checkout: use this Python and this tree.
            src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            cmd = [sys.executable, "-m", "lifeboat", "helper"]
            env["PYTHONPATH"] = src
            for k, v in os.environ.items():
                if k.startswith("LIFEBOAT_"):
                    env[k] = v
        else:
            cmd = [paths.BIN, "helper"]
        if os.geteuid() != 0:
            cmd = ["pkexec"] + cmd if not env else ["pkexec", "env", *[f"{k}={v}" for k, v in env.items()]] + cmd
        return cmd, env

    def _start(self) -> None:
        cmd, env = self._command()
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE)
        for k, v in env.items():
            launcher.setenv(k, v, True)
        try:
            self.proc = launcher.spawnv(cmd)
        except GLib.Error as exc:
            self._fail_all(f"Couldn't start the Lifeboat helper: {exc.message}")
            return
        self.stdin = self.proc.get_stdin_pipe()
        self.reader = Gio.DataInputStream.new(self.proc.get_stdout_pipe())
        self._read_next()

    def _read_next(self) -> None:
        self.reader.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_line)

    def _on_line(self, stream, result) -> None:
        try:
            line, _ = stream.read_line_finish_utf8(result)
        except GLib.Error:
            line = None
        if line is None:
            self._on_exit()
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            self._read_next()
            return
        if msg.get("id") == 0 and (msg.get("result") or {}).get("ready"):
            self.ready = True
            if self.on_state_change:
                self.on_state_change(True)
            for q in self.queue:
                self._write(q)
            self.queue.clear()
        elif msg.get("id") in self.pending:
            cb = self.pending.pop(msg["id"])
            if "error" in msg:
                cb(None, msg["error"])
            else:
                cb(msg.get("result"), None)
        self._read_next()

    def _on_exit(self) -> None:
        was_ready = self.ready
        self.ready = False
        self.proc = None
        if was_ready:
            msg = "The administrator session ended. Try again."
        else:
            msg = "Administrator access is needed for this. It was cancelled or isn't available."
        self._fail_all(msg)
        if self.on_state_change:
            self.on_state_change(False)

    def _fail_all(self, msg: str) -> None:
        pending, self.pending = self.pending, {}
        self.queue.clear()
        for cb in pending.values():
            cb(None, msg)

    def _write(self, line: str) -> None:
        try:
            self.stdin.write_all((line + "\n").encode(), None)
            self.stdin.flush(None)
        except GLib.Error:
            self._on_exit()

    def call(self, method: str, params: dict | None, callback) -> None:
        rid = self.next_id
        self.next_id += 1
        self.pending[rid] = callback
        line = json.dumps({"id": rid, "method": method, "params": params or {}})
        if self.proc is None:
            self.queue.append(line)
            self._start()
        elif not self.ready:
            self.queue.append(line)
        else:
            self._write(line)

    def stop(self) -> None:
        if self.proc is not None:
            try:
                self.stdin.close(None)
            except GLib.Error:
                pass


# ---------------------------------------------------------------------------
# read-only data (no password needed)

CONFIG_ERROR: str | None = None


def load_config() -> config.Config:
    """The config as written. On a syntax error, an empty config plus CONFIG_ERROR."""
    global CONFIG_ERROR
    try:
        cfg = config.load(check=False)
        CONFIG_ERROR = None
        return cfg
    except Exception as exc:
        CONFIG_ERROR = str(exc)
        return config.Config()


def config_dict() -> dict:
    return config.to_dict(load_config())


def jobs_overview() -> list[dict]:
    return overview.jobs(load_config())


def running() -> list[dict]:
    return status.running()


def history(limit: int = 200) -> list[dict]:
    return status.history(limit=limit)
