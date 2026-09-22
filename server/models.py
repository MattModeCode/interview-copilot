"""Two panes, answered in parallel.

Both panes run through warm `claude` CLI processes by default -- no API key,
using the existing subscription login. Sonnet answers fast, Opus answers
deeper; disagreement between them is itself a signal that the question is
contested and worth hedging on out loud.

A pane can be switched to Gemini by setting PANE_B=gemini with a working
GEMINI_API_KEY. That path uses the SDK directly because Google discontinued
the Gemini CLI's free OAuth tier for individuals.
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


def pane_specs() -> list[tuple[str, str]]:
    """[(pane_id, backend_spec)] -- backend is 'claude:<model>' or 'gemini'."""
    return [("a", config.PANE_A), ("b", config.PANE_B)]


def pane_label(spec: str) -> str:
    if spec.startswith("claude:"):
        return "Claude " + spec.split(":", 1)[1].capitalize()
    if spec == "gemini":
        return config.GEMINI_MODEL
    return spec


def start_pools() -> None:
    """Boot the warm sessions. Call this when capture starts, not on trigger."""
    for _, spec in pane_specs():
        if not spec.startswith("claude:"):
            continue
        if not shutil.which(config.CLAUDE_BIN) and not config.CLAUDE_BIN.startswith("/"):
            raise ProviderError(f"claude CLI not found: {config.CLAUDE_BIN}")
        _sessions.ensure(spec.split(":", 1)[1], SYSTEM)


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
            # 2.5-flash thinks by default, which cost ~5s to first token in
            # testing and bought nothing for this short structured format.
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    async for chunk in stream:
        if chunk.text:
            yield chunk.text


def _backend(spec: str):
    if spec.startswith("claude:"):
        model = spec.split(":", 1)[1]
        return lambda msg: _claude_cli(model, msg)
    if spec == "gemini":
        return _gemini
    raise ProviderError(f"unknown backend: {spec}")


async def answer(transcript: str, window_seconds: int, emit) -> None:
    user_msg = build_user_message(transcript, window_seconds)

    async def run(pane: str, spec: str) -> None:
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        first = True
        try:
            fn = _backend(spec)
            async for delta in fn(user_msg):
                if first:
                    await emit(pane, "first_token",
                               {"ms": int((loop.time() - t0) * 1000)})
                    first = False
                await emit(pane, "delta", {"text": delta})
            await emit(pane, "done", {"ms": int((loop.time() - t0) * 1000)})
        except Exception as e:
            await emit(pane, "error", {"message": f"{type(e).__name__}: {e}"})

    await asyncio.gather(*(run(p, s) for p, s in pane_specs()))
