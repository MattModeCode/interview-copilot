"""Parallel streaming answers from Claude and Gemini.

Both providers are wrapped to the same shape: an async generator of text
deltas. The caller races them concurrently and fans the deltas out over the
websocket tagged by pane, so whichever model is faster starts filling its
pane immediately rather than waiting for the other.
"""
import asyncio
import os
import shutil
from typing import AsyncIterator

from . import config
from .prompt import SYSTEM, build_user_message


class ProviderError(Exception):
    pass


# --- Claude -------------------------------------------------------------
async def _claude_api(user_msg: str) -> AsyncIterator[str]:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    async with client.messages.stream(
        model=config.ANTHROPIC_MODEL,
        max_tokens=config.MAX_TOKENS,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    ) as stream:
        async for text in stream.text_stream:
            yield text


async def _claude_cli(user_msg: str) -> AsyncIterator[str]:
    """Fallback when no ANTHROPIC_API_KEY: drive the `claude` CLI headless.

    Uses the existing subscription auth, but pays ~5s of process startup on
    every trigger. Usable in a pinch, not what you want mid-interview.
    """
    exe = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    if not os.path.exists(exe):
        raise ProviderError("no ANTHROPIC_API_KEY and no `claude` CLI on PATH")
    proc = await asyncio.create_subprocess_exec(
        exe, "-p", "--model", "sonnet", "--append-system-prompt", SYSTEM,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout
    proc.stdin.write(user_msg.encode())
    await proc.stdin.drain()
    proc.stdin.close()
    while True:
        chunk = await proc.stdout.read(256)
        if not chunk:
            break
        yield chunk.decode(errors="replace")
    await proc.wait()


async def claude_stream(user_msg: str) -> AsyncIterator[str]:
    if config.ANTHROPIC_API_KEY:
        async for t in _claude_api(user_msg):
            yield t
    else:
        async for t in _claude_cli(user_msg):
            yield t


# --- Gemini -------------------------------------------------------------
async def gemini_stream(user_msg: str) -> AsyncIterator[str]:
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
            # testing. The answer format is short and structured; thinking
            # bought nothing and the latency is the whole product.
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    async for chunk in stream:
        if chunk.text:
            yield chunk.text


PANES = {"claude": claude_stream, "gemini": gemini_stream}


async def answer(transcript: str, window_seconds: int, emit) -> None:
    """Run both panes concurrently, pushing deltas to `emit(pane, event, data)`."""
    user_msg = build_user_message(transcript, window_seconds)

    async def run(pane: str, fn) -> None:
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        first = True
        try:
            async for delta in fn(user_msg):
                if first:
                    await emit(pane, "first_token", {"ms": int((loop.time() - t0) * 1000)})
                    first = False
                await emit(pane, "delta", {"text": delta})
            await emit(pane, "done", {"ms": int((loop.time() - t0) * 1000)})
        except Exception as e:
            await emit(pane, "error", {"message": f"{type(e).__name__}: {e}"})

    await asyncio.gather(*(run(p, f) for p, f in PANES.items()))
