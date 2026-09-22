"""Endpointing: cut a continuous PCM stream into segments at pauses.

The old pipeline let ffmpeg's segment muxer cut a file every three seconds on
a timer. A timer knows nothing about speech, so cuts landed inside words, and
such a word is destroyed twice over -- half in a chunk that ends mid-phoneme,
half in one that starts mid-phoneme, and neither half decodes. On the fixture
set that cost three whole words and turned "requests per second" into
"requests per session. Second".

So we cut where the speaker stops instead. Energy is enough for this: the
signal is a single remote speaker on a call, and the only decision being made
is "is this gap big enough to be safe", not "is this speech". Silero still
runs inside faster-whisper afterwards; this stage only places the knife.

All audio is 16 kHz mono signed 16-bit little-endian -- what ffmpeg is told to
emit and what Whisper wants.
"""
import numpy as np

from . import config

BYTES_PER_SAMPLE = 2
FRAME_MS = 20
FULL_SCALE = 32768.0


def _frame_bytes(ms: int) -> int:
    return int(config.SAMPLE_RATE * ms / 1000) * BYTES_PER_SAMPLE


def rms(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    return float(np.sqrt(np.mean(a * a)))


def to_float32(pcm: bytes) -> np.ndarray:
    """PCM bytes to the [-1, 1] float array faster-whisper takes."""
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / FULL_SCALE


def duration(pcm: bytes) -> float:
    return len(pcm) / BYTES_PER_SAMPLE / config.SAMPLE_RATE


class Segmenter:
    """Feed PCM in, get complete utterances out.

    Holds an adaptive noise floor so it works on a quiet call and a loud one
    without a hand-tuned threshold. The floor falls quickly and rises very
    slowly: a long silence should lower the bar for what counts as speech,
    while a burst of speech must never raise it.
    """

    def __init__(self) -> None:
        self.frame = _frame_bytes(FRAME_MS)
        self.hold = max(1, config.SILENCE_HOLD_MS // FRAME_MS)
        self.min_speech = max(1, config.MIN_SPEECH_MS // FRAME_MS)
        self.preroll = max(1, config.PREROLL_MS // FRAME_MS)
        self.max_frames = max(
            self.hold + 1,
            int(config.SEGMENT_MAX_SECONDS * 1000) // FRAME_MS,
        )
        self.cut_window = max(1, config.CUT_SEARCH_MS // FRAME_MS)
        self.gap_frames = max(1, config.MIN_GAP_MS // FRAME_MS)
        self.hard_max_frames = self.max_frames + max(
            1, int(config.SEGMENT_GRACE_SECONDS * 1000) // FRAME_MS
        )

        self._buf = bytearray()
        self._pre: list[tuple[bytes, float]] = []
        self._seg: list[tuple[bytes, float]] = []
        self._in_speech = False
        self._silence = 0
        self._speech = 0
        self._floor = float(config.VAD_NOISE_FLOOR)

    # -- the noise floor ---------------------------------------------------
    def _is_speech(self, level: float) -> bool:
        return level > max(config.VAD_NOISE_FLOOR, self._floor * config.VAD_MARGIN)

    def _track(self, level: float) -> None:
        self._floor = (
            0.7 * self._floor + 0.3 * level
            if level < self._floor
            else 0.999 * self._floor + 0.001 * level
        )
        self._floor = max(self._floor, 1.0)

    # -- the stream --------------------------------------------------------
    def feed(self, pcm: bytes) -> list[bytes]:
        """Every segment that ended inside this block of audio."""
        self._buf.extend(pcm)
        out: list[bytes] = []
        while len(self._buf) >= self.frame:
            frame = bytes(self._buf[: self.frame])
            del self._buf[: self.frame]
            seg = self._push(frame)
            if seg:
                out.append(seg)
        return out

    def _push(self, frame: bytes) -> bytes | None:
        level = rms(frame)
        speech = self._is_speech(level)
        if not speech:
            self._track(level)

        if not self._in_speech:
            if not speech:
                self._pre.append((frame, level))
                del self._pre[: -self.preroll]
                return None
            # Open the segment backdated by the preroll: the first consonant
            # is quiet, which is exactly why the detector fired late, and it
            # belongs inside the segment rather than in front of it.
            self._seg = self._pre
            self._pre = []
            self._in_speech = True
            self._silence = 0
            self._speech = 0

        self._seg.append((frame, level))
        if speech:
            self._speech += 1
            self._silence = 0
        else:
            self._silence += 1
            if self._silence >= self.hold:
                return self._close()

        if len(self._seg) >= self.max_frames:
            return self._close(forced=True)
        return None

    def _best_gap(self, seg: list[tuple[bytes, float]]) -> int | None:
        """Index of the middle of the longest sub-threshold run near the tail.

        The quietest single frame is not good enough: inside a word there is
        always one frame quieter than its neighbours, and cutting there splits
        the word. A run of frames that are all below the speech threshold is a
        real gap between words, and its midpoint is the safe place.
        """
        start = max(0, len(seg) - self.cut_window)
        best = best_len = 0
        run = 0
        for i in range(start, len(seg)):
            if self._is_speech(seg[i][1]):
                run = 0
                continue
            run += 1
            if run > best_len:
                best_len, best = run, i - run // 2
        return best if best_len >= self.gap_frames else None

    def _close(self, forced: bool = False) -> bytes | None:
        """End the open segment. A forced cut splits at the quietest frame.

        Forced cuts happen when somebody talks for SEGMENT_MAX_SECONDS without
        a real pause. Cutting at the tail would land mid-word for the same
        reason the old timer did, so we search the last CUT_SEARCH_MS for the
        quietest frame: the smallest gap between two words is still far
        quieter than either of them.
        """
        seg, tail = self._seg, []
        spoken = self._speech
        if forced:
            idx = self._best_gap(seg)
            if idx is None:
                # Still no gap. Let them keep talking, up to the hard ceiling
                # -- a late cut costs latency, a cut inside a word costs the
                # word, and the word is worth more.
                if len(seg) < self.hard_max_frames:
                    return None
                start = max(0, len(seg) - self.cut_window)
                window = seg[start:]
                idx = start + min(range(len(window)), key=lambda i: window[i][1])
            seg, tail = seg[:idx], seg[idx:]

        audio = b"".join(f for f, _ in seg)

        if tail:
            # The speaker has not stopped: the remainder is the next segment,
            # already open, so nothing is stranded between the two.
            self._seg = tail
            self._in_speech = True
            self._silence = 0
            self._speech = sum(1 for _, lv in tail if self._is_speech(lv))
        else:
            self._seg = []
            self._pre = []
            self._in_speech = False
            self._silence = 0
            self._speech = 0

        if spoken < self.min_speech or not audio:
            return None
        return audio

    def flush(self) -> bytes | None:
        """Close whatever is open. Called when the stream ends."""
        if not self._seg:
            return None
        return self._close()
