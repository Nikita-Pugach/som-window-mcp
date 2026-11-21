"""Parent side of the UIA worker: persistent process, hard timeouts, respawn."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

DEFAULT_TIMEOUT = 8.0


class WorkerError(RuntimeError):
    """Raised when the worker timed out, died, or answered with an error."""


class UiaWorker:
    """A long-lived UIA worker process.

    Spawning a fresh interpreter per call costs 1-2 s (imports plus comtypes
    binding generation), so the process is reused. On timeout it is killed and
    transparently respawned on the next request.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()

    # -- process lifecycle -------------------------------------------------
    def _spawn(self) -> None:
        script = Path(__file__).with_name("uia_worker.py")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self._proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,  # never let a chatty child fill a pipe
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
            creationflags=flags,
        )

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass

    def close(self) -> None:
        with self._lock:
            self._kill()

    # -- request/response --------------------------------------------------
    def request(self, payload: dict, timeout: float | None = None) -> dict:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._spawn()
            proc = self._proc
            assert proc is not None and proc.stdin and proc.stdout

            try:
                proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            except Exception as exc:
                self._kill()
                raise WorkerError(f"worker write failed: {exc}") from exc

            box: dict[str, object] = {}

            def read_line() -> None:
                try:
                    box["line"] = proc.stdout.readline()
                except Exception as exc:  # pragma: no cover
                    box["error"] = exc

            reader = threading.Thread(target=read_line, daemon=True)
            reader.start()
            reader.join(timeout if timeout is not None else self._timeout)

            if reader.is_alive():
                # A blocked COM call cannot be cancelled; the process must die.
                self._kill()
                raise WorkerError(
                    "UI Automation did not respond in time; the worker was killed. "
                    "The target window is probably hung or showing a modal dialog."
                )

            if "error" in box:
                self._kill()
                raise WorkerError(f"worker read failed: {box['error']}")

            line = box.get("line")
            if not isinstance(line, str) or not line.strip():
                self._kill()
                raise WorkerError("worker exited without a response")

            try:
                response = json.loads(line)
            except Exception as exc:
                raise WorkerError(f"worker sent invalid JSON: {exc}") from exc

            if not response.get("ok", False):
                raise WorkerError(str(response.get("error", "unknown worker error")))
            return response

    def tree(
        self,
        hwnd: int,
        max_depth: int = 8,
        max_nodes: int = 600,
        include_text: bool = False,
        timeout: float | None = None,
    ) -> dict:
        return self.request(
            {
                "cmd": "tree",
                "hwnd": hwnd,
                "max_depth": max_depth,
                "max_nodes": max_nodes,
                "include_text": include_text,
            },
            timeout=timeout,
        )
