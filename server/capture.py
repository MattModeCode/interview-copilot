"""Audio capture -> continuous PCM -> endpointed utterances -> transcript.

ffmpeg runs once and writes raw 16 kHz mono PCM to a pipe. Nothing on disk,
nothing cut on a timer: a reader thread hands the stream to the segmenter,
which cuts it at pauses, and each finished utterance goes straight to the
in-process Whisper model.

Latency is now tied to how the person talks rather than to a fixed chunk. A
question that ends with a pause -- which is every question -- is decoded as
soon as that pause is SILENCE_HOLD_MS long, so the tail of a question lands
sooner than the old three-second slice could deliver it. A speaker who never
pauses is force-cut at SEGMENT_MAX_SECONDS, at the quietest point available.
"""
import signal
import subprocess
import threading

from . import config
from .stt import Transcriber

READ_BYTES = 4096


class Capture:
    def __init__(self, transcript, on_segment=None) -> None:
        self.transcript = transcript
        self.on_segment = on_segment
        self.stt = Transcriber()
        self._ff: subprocess.Popen | None = None
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
        # Loading the model is the slow part and it must fail loudly here,
        # before ffmpeg is holding the audio device, not silently mid-call.
        self.stt.start()
        self.stt.reset_context()
        self._ff = subprocess.Popen(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "avfoundation",
                "-i", f":{config.AUDIO_DEVICE}",
                "-ac", "1", "-ar", str(config.SAMPLE_RATE),
                "-f", "s16le", "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.running = True
        for target in (self._read_audio, self._watch_ffmpeg):
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
        for th in self._threads:
            th.join(timeout=2)
        self._threads.clear()
        # Whatever was mid-sentence when the button was pressed is still
        # speech, and the operator may well trigger on it.
        try:
            for text in self.stt.flush():
                self._emit(text)
        except Exception as err:
            self.error = f"stt: {err}"
        self._ff = None

    def flush_now(self) -> int:
        """Close the open utterance and emit it. Returns segments emitted.

        The trigger calls this: a speaker who has not paused yet is holding
        the end of the question inside an open segment, and waiting for their
        pause is waiting for the wrong thing.
        """
        if not self.running:
            return 0
        try:
            texts = self.stt.flush()
        except Exception as err:
            self.error = f"stt: {err}"
            return 0
        for text in texts:
            self._emit(text)
        return len(texts)

    # -- workers -----------------------------------------------------------
    def _read_audio(self) -> None:
        stream = self._ff.stdout if self._ff else None
        if stream is None:
            return
        while not self._stop.is_set():
            try:
                block = stream.read(READ_BYTES)
            except (OSError, ValueError):
                break
            if not block:
                break
            try:
                for text in self.stt.feed(block):
                    self._emit(text)
            except Exception as err:  # never let one bad decode end capture
                self.error = f"stt: {err}"
        if self.stt.error and not self.error:
            self.error = self.stt.error

    def _watch_ffmpeg(self) -> None:
        """Surface ffmpeg dying (wrong device index, device in use, no perms)."""
        if not self._ff or not self._ff.stderr:
            return
        err = self._ff.stderr.read()
        if self._stop.is_set():
            return
        msg = (err or b"").decode(errors="replace").strip()
        self.error = msg or "ffmpeg exited unexpectedly"
        self.running = False

    def _emit(self, text: str) -> None:
        seg = self.transcript.add(text)
        if seg and self.on_segment:
            try:
                self.on_segment(seg)
            except Exception:
                pass
