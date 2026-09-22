"""The `api` backend: Anthropic Messages API, streaming, with a cached prefix.

This is the backend a stranger cloning the repo actually uses, because the
`cli` backend needs a Claude subscription login on the machine. It has to hit
the same latency target: the candidate is sitting in silence while it runs.

Two things make that happen.

1. NO PER-TURN STARTUP. There is no process to boot, so the ~2.4s the CLI
   backend pays once at capture start is simply not paid here at all. The
   client is built once and reused, so the TLS connection is warm too.

2. PROMPT CACHING ON THE STATIC PREFIX. The system prompt is large (the whole
   question-finding rubric) and identical on every turn of an interview, as is
   the domain brief baked into it. It is sent as one cache_control block, so
   after the first turn it is read from cache instead of re-processed. That is
   most of the time-to-first-token, and most of the cost.

Unlike the CLI backend, each turn is a FRESH request carrying only this
window's transcript. There is no conversation to anchor a later answer to an
earlier one, so there is nothing to recycle -- RECYCLE_AFTER does not apply
here. The independence that the CLI backend has to buy with a periodic
recycle is free.
"""
import threading
from typing import AsyncIterator

from . import config

# Anthropic's own retry/limit signals. A 429 or an overloaded error mid-answer
# is the one failure the candidate must understand at a glance, so it is
# surfaced as a limit rather than a generic error, exactly like the CLI
# backend's subscription-limit path.
_LIMIT_MARKERS = (
    "rate_limit", "rate limit", "overloaded", "quota",
    "credit balance", "too many requests",
)


def is_limit(msg: str) -> bool:
    m = (msg or "").lower()
    return any(k in m for k in _LIMIT_MARKERS)


class ApiSession:
    """Stateless answering over the Messages API, with the pool interface.

    Presents the same surface as cli_pool.WarmSession (start / ask / status
    fields) so server/models.py and the UI do not care which backend is live.
    """

    def __init__(self, model: str, system_prompt: str) -> None:
        self.model = model
        self.system_prompt = system_prompt
        # "start() has run", NOT "the backend works" -- unlike the CLI
        # backend's ready, which waits on a real warmup turn. Usability is
        # .failed, and status() combines the two. Read them together.
        #
        # threading.Event, not asyncio: start() runs in a worker thread
        # (asyncio.to_thread from app.py) where there is no loop.
        self.ready = threading.Event()
        self.failed: str | None = None
        self.limit_hit = False
        self.turns = 0
        self._client = None

    # -- lifecycle ---------------------------------------------------------
    def start(self, timeout: float = 120.0) -> None:
        # timeout is accepted and ignored, so the signature matches
        # WarmSession.start() -- there is no warmup round trip to wait on.
        """Build the client. No network turn is burned here.

        The CLI backend has to spend a real warmup turn to prove auth, because
        a subscription's session limit is invisible until you ask something.
        An API key has no such hidden state: a bad key or an empty balance
        fails loudly on the first real turn with a message that says which.
        Preflight makes that turn explicitly; capture start should not.
        """
        self.failed = None
        self.limit_hit = False
        self.turns = 0
        if not config.ANTHROPIC_API_KEY:
            self.failed = (
                "ANTHROPIC_API_KEY is not set. Put a key in .env, or set "
                "BACKEND=cli to use a logged-in claude CLI instead."
            )
            self.ready.set()
            return
        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            self.failed = (
                "the anthropic package is missing -- "
                "./.venv/bin/pip install -r requirements.txt"
            )
            self.ready.set()
            return
        self._client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
        self.ready.set()

    def close(self) -> None:
        self._client = None
        self.ready.clear()

    def needs_recycle(self) -> bool:
        return False  # nothing accumulates; see the module docstring

    # -- answering ---------------------------------------------------------
    async def ask(self, text: str) -> AsyncIterator[str]:
        if self.failed:
            raise RuntimeError(self.failed)
        if self._client is None:
            raise RuntimeError("api backend was never started")
        self.turns += 1
        try:
            async with self._client.messages.stream(
                model=self.model,
                max_tokens=config.MAX_TOKENS,
                # No temperature override: the SDK's stream() does not accept
                # one, and the cli backend never set it either, so leaving it
                # at the default is also the way the two backends stay
                # comparable.
                system=[
                    {
                        "type": "text",
                        "text": self.system_prompt,
                        # The whole point: identical every turn, so cache it.
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": text}],
            ) as stream:
                async for delta in stream.text_stream:
                    if delta:
                        yield delta
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"[:300]
            self.limit_hit = is_limit(msg)
            raise RuntimeError(msg) from e

    def status(self) -> dict:
        return {
            "ready": self.ready.is_set() and not self.failed,
            "turns": self.turns,
            "error": self.failed,
            "limit_hit": self.limit_hit,
        }
