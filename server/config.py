import os
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


# --- audio capture ---
# Index from: ffmpeg -f avfoundation -list_devices true -i ""
AUDIO_DEVICE = os.environ.get("AUDIO_DEVICE", "1")  # BlackHole 2ch
CHUNK_SECONDS = _int("CHUNK_SECONDS", 3)
SAMPLE_RATE = 16000

# --- speech to text ---
WHISPER_SERVER = os.environ.get("WHISPER_SERVER", "http://127.0.0.1:8178")
WHISPER_BIN = os.environ.get("WHISPER_BIN", "/opt/homebrew/bin/whisper-server")
WHISPER_MODEL = os.environ.get(
    "WHISPER_MODEL", "models/ggml-large-v3-turbo.bin"
)

# --- transcript window ---
# How far back the answer call looks. 90s comfortably covers a long
# multi-part question plus the preamble that gives it context.
WINDOW_SECONDS = _int("WINDOW_SECONDS", 90)
TRANSCRIPT_MAX_SECONDS = _int("TRANSCRIPT_MAX_SECONDS", 3600)

# --- models ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
MAX_TOKENS = _int("MAX_TOKENS", 1200)

# --- server ---
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = _int("PORT", 8477)
HOTKEY_ENABLED = os.environ.get("HOTKEY_ENABLED", "1") == "1"
