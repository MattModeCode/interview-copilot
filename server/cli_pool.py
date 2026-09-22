"""Warm `claude` CLI sessions, so a trigger never pays process startup.

A cold `claude -p` costs ~5s: node boot, settings and plugin load, MCP server
startup, auth. Stripping all of that with flags gets it to ~2.4s, still far
too slow to sit in silence for mid-interview.

So we stop paying it per answer. `--input-format stream-json` keeps one
process alive across many turns: startup and the first connection are paid
once, when capture starts, and every later turn is ~620ms to first token --
as fast as a raw HTTP call, with no API key, on the existing subscription.

ONE PERSISTENT SESSION PER MODEL, not a process per trigger. An earlier
design spawned a fresh process for each answer to keep contexts clean; it
cost an extra warmup round trip every single time and burned through the
subscription's session limit roughly twice as fast as necessary. Reusing the
session also keeps the prompt cache warm, which is most of the latency win.

The cost of reuse is that previous answers stay in context and could anchor
later ones, so the session is recycled every RECYCLE_AFTER turns.
"""
import asyncio
import json
import subprocess
import threading
from typing import AsyncIterator

from . import config

_MCP_OFF = '{"mcpServers":{}}'

# Phrases the CLI uses when the subscription is out of quota. Worth detecting
# precisely: mid-interview this is the one failure you must understand in one
# glance, because no amount of retrying will fix it.
_LIMIT_MARKERS = (
    "session limit", "usage limit", "rate limit", "quota",
    "reset at", "resets ", "upgrade to",
)


def _is_limit(msg: str) -> bool:
    m = (msg or "").lower()
    return any(k in m for k in _LIMIT_MARKERS)


def _args(model: str, system_prompt: str) -> list[str]:
    return [
        config.CLAUDE_BIN,
        "-p",
        "--model", model,
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        # Everything below is dead weight here and costs seconds of startup.
        "--strict-mcp-config", "--mcp-config", _MCP_OFF,
        "--setting-sources", "",
        "--system-prompt", system_prompt,
        "--restricted",
        "--no-session-persistence",
    ]


class WarmSession:
    """One long-lived `claude` process for one model."""

    def __init__(self, model: str, system_prompt: str) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self.proc: subprocess.Popen | None = None
        self.ready = threading.Event()
        self.failed: str | None = None
        self.limit_hit = False
        self.turns = 0
        self._lines: list[str] = []
        self._cursor = 0
        self._lock = threading.Lock()
        self._busy = asyncio.Lock()

    # -- process plumbing --------------------------------------------------
    def _spawn(self) -> None:
        self.proc = subprocess.Popen(
            _args(self.model, self.system_prompt),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        with self._lock:
            self._lines, self._cursor = [], 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        p = self.proc
        if not p or not p.stdout:
            return
        for line in p.stdout:
            with self._lock:
                self._lines.append(line)
        if p.poll() not in (0, None) and not self.ready.is_set():
            err = (p.stderr.read() if p.stderr else "") or ""
            self.failed = err.strip()[:300] or f"claude exited {p.poll()}"
            self.ready.set()

    def _send(self, text: str) -> None:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("claude process is not running")
        msg = {"type": "user",
               "message": {"role": "user",
                           "content": [{"type": "text", "text": text}]}}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _drain(self) -> list[dict]:
        with self._lock:
            new = self._lines[self._cursor:]
            self._cursor = len(self._lines)
        out = []
        for line in new:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return out

    # -- lifecycle ---------------------------------------------------------
    def start(self, timeout: float = 120.0) -> None:
        """Spawn and burn ONE warmup turn. Paid once per interview, not per answer."""
        self.ready.clear()
        self.failed = None
        self.turns = 0
        try:
            self._spawn()
            self._send("ping")
        except Exception as e:
            self.failed = f"could not start claude: {e}"
            self.ready.set()
            return
        waited = 0.0
        while waited < timeout:
            if self.failed:
                break
            for ev in self._drain():
                if ev.get("type") == "result":
                    if ev.get("is_error"):
                        msg = str(ev.get("result"))[:300]
                        self.failed = msg
                        self.limit_hit = _is_limit(msg)
                    self.ready.set()
                    return
            threading.Event().wait(0.05)
            waited += 0.05
        self.failed = self.failed or "claude did not respond to warmup"
        self.ready.set()

    async def ask(self, text: str) -> AsyncIterator[str]:
        if self.failed:
            raise RuntimeError(self.failed)
        async with self._busy:
            self._drain()  # discard anything left over from the previous turn
            self._send(text)
            self.turns += 1
            while True:
                for ev in self._drain():
                    t = ev.get("type")
                    if t == "stream_event":
                        e = ev.get("event", {})
                        if e.get("type") == "content_block_delta":
                            d = e.get("delta", {})
                            if d.get("type") == "text_delta" and d.get("text"):
                                yield d["text"]
                    elif t == "result":
                        if ev.get("is_error"):
                            msg = str(ev.get("result"))[:300]
                            self.limit_hit = _is_limit(msg)
                            raise RuntimeError(msg)
                        return
                if self.proc and self.proc.poll() is not None:
                    raise RuntimeError("claude process exited mid-answer")
                await asyncio.sleep(0.02)

    def needs_recycle(self) -> bool:
        return self.turns >= config.RECYCLE_AFTER

    def close(self) -> None:
        p = self.proc
        self.proc = None
        if not p:
            return
        try:
            if p.stdin and not p.stdin.closed:
                p.stdin.close()
        except Exception:
            pass
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()

    def recycle_async(self) -> None:
        """Replace the session in the background, between questions."""
        def work() -> None:
            self.close()
            self.start()
        threading.Thread(target=work, daemon=True).start()


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, WarmSession] = {}

    def ensure(self, model: str, system_prompt: str) -> WarmSession:
        s = self._sessions.get(model)
        if s is None:
            s = WarmSession(model, system_prompt)
            self._sessions[model] = s
            threading.Thread(target=s.start, daemon=True).start()
        return s

    def get(self, model: str) -> WarmSession | None:
        return self._sessions.get(model)

    def status(self) -> dict:
        return {
            m: {
                "ready": s.ready.is_set() and not s.failed,
                "turns": s.turns,
                "error": s.failed,
                "limit_hit": s.limit_hit,
            }
            for m, s in self._sessions.items()
        }

    def close(self) -> None:
        for s in self._sessions.values():
            s.close()
        self._sessions.clear()
