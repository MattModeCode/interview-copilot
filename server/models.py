"""Single pane: a warm `claude` CLI session, no API key.

Sonnet only. A Haiku second pane was tried and dropped again by request --
Sonnet's judgment on which sentence in a noisy transcript is actually "the
question" was the thing worth trusting most, and running two panes doubled
the subscription-quota burn for a signal (Haiku agreeing or disagreeing)
that wasn't worth that cost.

Gemini stays available as a manual override if you ever want a genuinely
different model's opinion: set CLAUDE_MODEL is irrelevant then, instead the
_gemini backend below is wired but unused by default -- kept because Google
discontinued the Gemini CLI's free OAuth tier for individuals, so this SDK
path is the only way to reach Gemini here at all, and ripping it out would
mean re-deriving it from scratch if it's ever wanted again.
"""
import asyncio
import shutil
from typing import AsyncIterator

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


# --- backends -----------------------------------------------------------
async def _claude_cli(model: str, user_msg: str) -> AsyncIterator[str]:
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


async def _gemini(user_msg: str) -> AsyncIterator[str]:
    """Unused by default. Wired for the day a second, genuinely different
    model's opinion is wanted again -- set GEMINI_API_KEY and call this
    directly rather than through answer(), which is single-pane now."""
    if not config.GEMINI_API_KEY:
        raise ProviderError("GEMINI_API_KEY is not set")
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=config.GEMINI_API_KEY)
    stream = await client.aio.models.generate_content_stream(
        model=config.GEMINI_MODEL,
        contents=user_msg,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            max_output_tokens=config.MAX_TOKENS,
            temperature=0.3,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    async for chunk in stream:
        if chunk.text:
            yield chunk.text


async def answer(transcript: str, window_seconds: int, emit) -> None:
    user_msg = build_user_message(transcript, window_seconds)
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    first = True
    try:
        async for delta in _claude_cli(config.CLAUDE_MODEL, user_msg):
            if first:
                await emit("a", "first_token", {"ms": int((loop.time() - t0) * 1000)})
                first = False
            await emit("a", "delta", {"text": delta})
        await emit("a", "done", {"ms": int((loop.time() - t0) * 1000)})
    except Exception as e:
        await emit("a", "error", {"message": f"{type(e).__name__}: {e}"})
