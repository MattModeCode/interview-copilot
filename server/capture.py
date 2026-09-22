"""Audio capture -> chunked WAV -> whisper-server -> transcript buffer.

ffmpeg runs once, continuously, and uses its segment muxer to drop a finished
WAV every CHUNK_SECONDS. A watcher thread picks up each completed file and
POSTs it to whisper-server, which keeps the model resident so we pay the load
cost once instead of per chunk.

Chunking (rather than true streaming) costs up to CHUNK_SECONDS of latency on
the newest words. That is fine: the trigger looks back over a 90s window, so a
few seconds of lag at the tail never loses the question.
"""
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx

from . import config

# whisper emits these on silence or music. They are model artifacts, not speech,
# and they poison the transcript window if they get through.
_HALLUCINATIONS = {
    "", "you", "thank you", "thanks for watching", "thank you.", "you.",
    "thanks for watching!", "please subscribe", "[blank_audio]", "(silence)",
    "bye", "bye.", "subtitles by the amara.org community", "so", "so.",
    "[music]", "(upbeat music)", "[ silence ]", ".", "...", "1", "okay",
}


def _is_noise(text: str) -> bool:
    t = text.strip().lower()
    if t in _HALLUCINATIONS:
        return True
    if not re.search(r"[a-z]", t):
        return True
    # A lone bracketed tag like [BLANK_AUDIO] or (wind blowing).
    if re.fullmatch(r"[\[\(].*[\]\)]", t):
        return True
    return False


class WhisperServer:
    """Owns the whisper-server subprocess."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        if not Path(config.WHISPER_MODEL).exists():
            raise FileNotFoundError(f"whisper model missing: {config.WHISPER_MODEL}")
        if self._alive():
            return  # somebody already started one
        port = config.WHISPER_SERVER.rsplit(":", 1)[-1]
        self.proc = subprocess.Popen(
            [
                config.WHISPER_BIN,
                "-m", config.WHISPER_MODEL,
                "--port", port,
                "--host", "127.0.0.1",
                "-t", "8",
                "--no-timestamps",
                "-l", "en",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(120):
            if self._alive():
                return
            time.sleep(0.5)
        raise RuntimeError("whisper-server did not come up within 60s")

    def _alive(self) -> bool:
        try:
            httpx.get(config.WHISPER_SERVER + "/", timeout=1.0)
            return True
        except Exception:
            return False

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class Capture:
    def __init__(self, transcript, on_segment=None) -> None:
        self.transcript = transcript
        self.on_segment = on_segment
        self.whisper = WhisperServer()
        self._ff: subprocess.Popen | None = None
        self._dir: str | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.running = False
        self.error: str | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self.running:
            return
        self.error = None
        self._stop.clear()
        self.whisper.start()
        self._dir = tempfile.mkdtemp(prefix="icopilot-audio-")
        self._ff = subprocess.Popen(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "avfoundation",
                "-i", f":{config.AUDIO_DEVICE}",
                "-ac", "1", "-ar", str(config.SAMPLE_RATE),
                "-f", "segment",
                "-segment_time", str(config.CHUNK_SECONDS),
                "-reset_timestamps", "1",
                os.path.join(self._dir, "chunk_%06d.wav"),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.running = True
        for target in (self._watch_files, self._watch_ffmpeg):
            th = threading.Thread(target=target, daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self) -> None:
        self._stop.set()
        self.running = False
        if self._ff and self._ff.poll() is None:
            self._ff.send_signal(signal.SIGINT)
            try:
                self._ff.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._ff.kill()
        self.whisper.stop()
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    # -- workers -----------------------------------------------------------
    def _watch_ffmpeg(self) -> None:
        """Surface ffmpeg dying (wrong device index, device in use, no perms)."""
        if not self._ff:
            return
        _, err = self._ff.communicate()
        if self._stop.is_set():
            return
        msg = (err or b"").decode(errors="replace").strip()
        self.error = msg or "ffmpeg exited unexpectedly"
        self.running = False

    def _watch_files(self) -> None:
        seen: set[str] = set()
        while not self._stop.is_set():
            if not self._dir:
                break
            try:
                files = sorted(Path(self._dir).glob("chunk_*.wav"))
            except OSError:
                break
            # The newest file is still being written by ffmpeg; leave it.
            for path in files[:-1]:
                if path.name in seen:
                    continue
                seen.add(path.name)
                self._transcribe(path)
                path.unlink(missing_ok=True)
            time.sleep(0.25)

    def _transcribe(self, path: Path) -> None:
        try:
            if path.stat().st_size < 4000:  # header-only / near-empty
                return
            with open(path, "rb") as fh:
                r = httpx.post(
                    config.WHISPER_SERVER + "/inference",
                    files={"file": (path.name, fh, "audio/wav")},
                    data={"response_format": "json", "temperature": "0"},
                    timeout=60.0,
                )
            r.raise_for_status()
            text = (r.json().get("text") or "").strip()
        except Exception as e:  # a bad chunk must never kill the loop
            self.error = f"stt: {e}"
            return
        if _is_noise(text):
            return
        seg = self.transcript.add(text)
        if seg and self.on_segment:
            try:
                self.on_segment(seg)
            except Exception:
                pass
