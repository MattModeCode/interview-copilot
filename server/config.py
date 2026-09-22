import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# --- audio capture ---
# Index from: ffmpeg -f avfoundation -list_devices true -i ""
AUDIO_DEVICE = os.environ.get("AUDIO_DEVICE", "1")  # BlackHole 2ch
SAMPLE_RATE = 16000

# --- endpointing ---
# Audio is no longer cut on a timer. It is cut where the speaker pauses, which
# is the only place a cut is free. See server/vad.py for why.
SILENCE_HOLD_MS = _int("SILENCE_HOLD_MS", 600)     # pause that ends an utterance
MIN_SPEECH_MS = _int("MIN_SPEECH_MS", 240)         # shorter than this is a cough
PREROLL_MS = _int("PREROLL_MS", 320)               # kept from before speech starts
SEGMENT_MAX_SECONDS = _float("SEGMENT_MAX_SECONDS", 14.0)
CUT_SEARCH_MS = _int("CUT_SEARCH_MS", 2500)        # how far back to hunt for a gap
MIN_GAP_MS = _int("MIN_GAP_MS", 80)                # a gap this long is between words
SEGMENT_GRACE_SECONDS = _float("SEGMENT_GRACE_SECONDS", 6.0)
VAD_NOISE_FLOOR = _float("VAD_NOISE_FLOOR", 140.0)  # absolute RMS floor, 0-32768
VAD_MARGIN = _float("VAD_MARGIN", 2.6)             # times the adaptive floor

# --- speech to text ---
# faster-whisper (CTranslate2), in-process. No subprocess, no HTTP hop, and
# the model stays resident for the life of the server.
STT_MODEL = os.environ.get("STT_MODEL", "large-v3-turbo")
STT_DEVICE = os.environ.get("STT_DEVICE", "cpu")   # no Metal backend in CT2
STT_COMPUTE = os.environ.get("STT_COMPUTE", "int8")
STT_THREADS = _int("STT_THREADS", 8)
STT_CACHE = os.environ.get("STT_CACHE", "")        # empty = the HF default
STT_LANGUAGE = os.environ.get("STT_LANGUAGE", "en")
STT_BEAM = _int("STT_BEAM", 5)
STT_VAD_FILTER = os.environ.get("STT_VAD_FILTER", "1") == "1"
# Confidence gates. These replace the word blacklist that used to delete
# "okay", "so" and "bye" -- all of which are real interview speech.
STT_NO_SPEECH_MAX = _float("STT_NO_SPEECH_MAX", 0.6)
STT_LOGPROB_MIN = _float("STT_LOGPROB_MIN", -1.0)
STT_PROMPT_CHARS = _int("STT_PROMPT_CHARS", 600)   # ~the 224-token prompt window

# --- interview domain ---
# Free text, injected into the system prompt. Tells the model what field
# this interview is in and which terms are likely to get mis-transcribed --
# it uses this to silently correct STT homophone errors and to judge which
# vocabulary is actually in-scope. Lives in domain.md (a real brief needs
# paragraphs and a glossary, which a one-line .env var can't hold); the
# DOMAIN_CONTEXT env var overrides it if set, for a quick one-off swap.
_DOMAIN_FILE = ROOT / "domain.md"


def _load_domain() -> str:
    env = os.environ.get("DOMAIN_CONTEXT", "").strip()
    if env:
        return env
    if _DOMAIN_FILE.exists():
        return _DOMAIN_FILE.read_text().strip()
    return ""


DOMAIN_CONTEXT = _load_domain()


def _vocab_prompt(brief: str) -> str:
    """The decoder's prompt: the jargon out of domain.md, nothing else.

    Whisper's initial_prompt window is about 224 tokens, so the brief itself
    does not fit and would crowd out the rolling transcript anyway. What
    actually moves accuracy is the vocabulary -- the terms that have a common
    homophone ("rate equation" against "race equation"). Those are the ones a
    brief writes in bold, in backticks, in capitals, or as a glossary entry.
    """
    if not brief:
        return ""
    terms: list[str] = []

    def add(term: str) -> None:
        term = term.strip().strip("*`_-: ")
        if 1 < len(term) < 48 and term.lower() not in (t.lower() for t in terms):
            terms.append(term)

    # Emphasis and code spans first, everywhere in the brief. Doing these
    # before the glossary pattern matters: a line written as
    # "- **rate** vs **race**: homophone pair" holds two terms, and a
    # single span up to the colon would capture one garbled string instead.
    marked: list[tuple[int, int]] = []
    for m in re.finditer(r"\*\*(.+?)\*\*|`([^`]+)`", brief):
        add(next(g for g in m.groups() if g))
        marked.append(m.span())

    # Then glossary lines -- but only the ones that had no emphasis in them,
    # since those have already given up their terms more precisely.
    for m in re.finditer(r"^[ \t]*[-*][ \t]+([^:\n]{2,40}):", brief, re.M):
        if any(s < m.end(1) and m.start(1) < e for s, e in marked):
            continue
        add(m.group(1))

    for m in re.finditer(r"\b[A-Z]{2,8}\b", brief):
        add(m.group())
    if not terms:
        return ""
    return "Technical interview. Terms: " + ", ".join(terms[:40]) + "."


STT_PROMPT = os.environ.get("STT_PROMPT", "") or _vocab_prompt(DOMAIN_CONTEXT)

# --- transcript window ---
# How far back the answer call looks. 90s comfortably covers a long
# multi-part question plus the preamble that gives it context.
WINDOW_SECONDS = _int("WINDOW_SECONDS", 90)
TRANSCRIPT_MAX_SECONDS = _int("TRANSCRIPT_MAX_SECONDS", 3600)

# --- model ---
# Two answering backends, same interface, chosen by BACKEND:
#
#   api  (default)  the Anthropic Messages API with an ANTHROPIC_API_KEY.
#                   Works for anyone. Startup is free, the static prefix is
#                   cached, so first-token latency lands in the same place
#                   the CLI backend gets to by keeping a process warm.
#   cli             a warm `claude` CLI session on an existing Claude
#                   subscription login, no key. Only works if you have the
#                   CLI installed and logged in; burns subscription quota.
#
# See server/models.py. Everything downstream (prompt, streaming, auto-clear)
# is identical either way.
BACKEND = os.environ.get("BACKEND", "api").strip().lower()

# -- api backend --
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

# -- cli backend --
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")
# Resolved from PATH by default. Set CLAUDE_BIN only if `claude` is somewhere
# your shell cannot see.
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "").strip() or "claude"
# CLI only. Answers stay in the session context, so recycle it periodically to
# stop earlier answers anchoring later ones. Recycling happens in the
# background between questions and costs one warmup turn each -- don't set it
# low.
RECYCLE_AFTER = _int("RECYCLE_AFTER", 8)

MAX_TOKENS = _int("MAX_TOKENS", 1200)


def model_label() -> str:
    """What the UI and /health call the answering model."""
    return ANTHROPIC_MODEL if BACKEND == "api" else CLAUDE_MODEL

# --- server ---
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = _int("PORT", 8477)
HOTKEY_ENABLED = os.environ.get("HOTKEY_ENABLED", "1") == "1"
