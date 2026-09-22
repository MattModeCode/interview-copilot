"""One answering path, two interchangeable backends.

Both expose the same thing -- a stream of text deltas for one transcript
window -- so nothing downstream (the trigger, auto-clear, the UI) knows or
cares which is live. `BACKEND` in .env picks one:

  api   Anthropic Messages API + ANTHROPIC_API_KEY. The default, because it
        works for anyone who clones this. See server/api_client.py.
  cli   A warm `claude` CLI session on an existing subscription login, no key.
        See server/cli_pool.py for why it holds one process open.

Sonnet only, one pane. A Haiku second pane was tried and dropped: Sonnet's
judgment about which sentence in a noisy transcript is actually "the question"
was the thing worth trusting, and a second pane doubled the cost for a signal
(agreement or disagreement) that wasn't worth it. Gemini was tried too and is
gone -- Google discontinued the CLI's free OAuth tier for individuals, and an
API-key Gemini path would be a second key for a worse answer.
"""
import asyncio
import shutil
from typing import AsyncIterator

from . import config
from .api_client import ApiSession
from .cli_pool import SessionManager, WarmSession
from .prompt import SYSTEM, build_user_message


class ProviderError(Exception):
    pass


_sessions = SessionManager()          # cli backend
_api: ApiSession | None = None        # api backend


def _is_api() -> bool:
    # BACKEND is read from the environment once at import and never mutated,
    # so this is a constant for the life of the process. Nothing here keeps
    # _api and _sessions in sync, so it must stay that way -- a runtime
    # backend swap would need to tear both down first.
    return config.BACKEND == "api"


def start_pools() -> None:
    """Prepare the backend. Called when capture starts, not on trigger.

    For `cli` this is the expensive one -- process boot plus a warmup turn,
    paid once for the whole interview so no trigger ever waits for it.
    """
    global _api
    if config.BACKEND not in ("api", "cli"):
        raise ProviderError(
            f"BACKEND={config.BACKEND!r} is not valid; use 'api' or 'cli'"
        )
    if _is_api():
        # Called via asyncio.to_thread from app.py, so this runs off the event
        # loop. Safe because ApiSession.start() only builds an AsyncAnthropic,
        # whose httpx transport binds no loop until the first request, and the
        # global assignment is a single atomic write read later from the loop
        # thread. Anything here that needed a running loop would have to move.
        _api = ApiSession(config.ANTHROPIC_MODEL, SYSTEM)
        _api.start()
        if _api.failed:
            raise ProviderError(_api.failed)
        return
    if shutil.which(config.CLAUDE_BIN) is None:
        raise ProviderError(
            f"claude CLI not found: {config.CLAUDE_BIN!r}. Install it and log "
            "in, set CLAUDE_BIN, or use BACKEND=api with an API key."
        )
    _sessions.ensure(config.CLAUDE_MODEL, SYSTEM)


def make_session() -> ApiSession | WarmSession:
    """One fresh, independent session on the configured backend.

    The server keeps a single shared session; the test scripts want a
    throwaway per case. Both backends answer to the same four things --
    start(), .failed, ask(), close() -- so callers stay backend-blind.
    """
    if _is_api():
        return ApiSession(config.ANTHROPIC_MODEL, SYSTEM)
    return WarmSession(config.CLAUDE_MODEL, SYSTEM)


def stop_pools() -> None:
    global _api
    if _api is not None:
        _api.close()
        _api = None
    _sessions.close()


def pool_status() -> dict:
    """{model: {ready, turns, error, limit_hit}} -- the shape the UI reads."""
    if _is_api():
        return {config.ANTHROPIC_MODEL: _api.status()} if _api else {}
    return _sessions.status()


# --- backends -----------------------------------------------------------
async def _ask_api(user_msg: str) -> AsyncIterator[str]:
    if _api is None:
        raise ProviderError("api backend was never started")
    if _api.failed:
        raise ProviderError(_api.failed)
    async for t in _api.ask(user_msg):
        yield t


async def _ask_cli(model: str, user_msg: str) -> AsyncIterator[str]:
    sess = _sessions.get(model)
    if sess is None:
        raise ProviderError(f"session for {model} was never started")
    # The session boots in the background when capture starts; if the trigger
    # beats it, wait rather than failing.
    waited = 0.0
    while not sess.ready.is_set() and waited < 60.0:
        await asyncio.sleep(0.1)
        waited += 0.1
    if sess.failed:
        raise ProviderError(sess.failed)

    async for t in sess.ask(user_msg):
        yield t

    if sess.needs_recycle():
        sess.recycle_async()


async def answer(transcript: str, window_seconds: int, emit) -> None:
    user_msg = build_user_message(transcript, window_seconds)
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    first = True
    try:
        stream = (
            _ask_api(user_msg) if _is_api()
            else _ask_cli(config.CLAUDE_MODEL, user_msg)
        )
        async for delta in stream:
            if first:
                await emit("a", "first_token", {"ms": int((loop.time() - t0) * 1000)})
                first = False
            await emit("a", "delta", {"text": delta})
        await emit("a", "done", {"ms": int((loop.time() - t0) * 1000)})
    except Exception as e:
        await emit("a", "error", {"message": f"{type(e).__name__}: {e}"})
