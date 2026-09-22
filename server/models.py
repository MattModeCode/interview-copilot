"""Single pane: a warm `claude` CLI session, no API key.

A second pane was tried and dropped. Gemini CLI's free OAuth tier for
individuals is discontinued by Google; the Antigravity CLI has no headless
mode at all (confirmed: every flag -- -p, --print, --prompt, --headless,
--cli -- returns silently, and the binary is a thin launcher for the IDE
app, not an agent). Both would need a workaround the user didn't want, so
this is Sonnet alone rather than Sonnet paired with a worse substitute.
"""
import asyncio
import shutil

from . import config
from .cli_pool import SessionManager
from .prompt import SYSTEM, build_user_message


class ProviderError(Exception):
    pass


_sessions = SessionManager()


def start_pools() -> None:
    """Boot the warm session. Call this when capture starts, not on trigger."""
    if not shutil.which(config.CLAUDE_BIN) and not config.CLAUDE_BIN.startswith("/"):
        raise ProviderError(f"claude CLI not found: {config.CLAUDE_BIN}")
    _sessions.ensure(config.CLAUDE_MODEL, SYSTEM)


def stop_pools() -> None:
    _sessions.close()


def pool_status() -> dict:
    return _sessions.status()


async def answer(transcript: str, window_seconds: int, emit) -> None:
    user_msg = build_user_message(transcript, window_seconds)
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    first = True
    try:
        sess = _sessions.get(config.CLAUDE_MODEL)
        if sess is None:
            raise ProviderError("session was never started")
        waited = 0.0
        while not sess.ready.is_set() and waited < 60.0:
            await asyncio.sleep(0.1)
            waited += 0.1
        if sess.failed:
            raise ProviderError(sess.failed)

        async for delta in sess.ask(user_msg):
            if first:
                await emit("a", "first_token", {"ms": int((loop.time() - t0) * 1000)})
                first = False
            await emit("a", "delta", {"text": delta})
        await emit("a", "done", {"ms": int((loop.time() - t0) * 1000)})

        if sess.needs_recycle():
            sess.recycle_async()
    except Exception as e:
        await emit("a", "error", {"message": f"{type(e).__name__}: {e}"})
