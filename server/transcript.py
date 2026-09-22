"""Thread-safe rolling transcript buffer.

Segments are (monotonic_timestamp, text). The buffer is trimmed by age, not
by count, so `window()` always means the same wall-clock amount of speech
regardless of how fast people were talking.
"""
import threading
import time
from dataclasses import dataclass, asdict

from . import config


@dataclass
class Segment:
    t: float          # seconds since session start
    text: str
    wall: float       # unix time, for the UI clock


class Transcript:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._segments: list[Segment] = []
        self._start = time.monotonic()

    def add(self, text: str) -> Segment | None:
        text = text.strip()
        if not text:
            return None
        now = time.monotonic()
        seg = Segment(t=now - self._start, text=text, wall=time.time())
        with self._lock:
            self._segments.append(seg)
            cutoff = seg.t - config.TRANSCRIPT_MAX_SECONDS
            if self._segments[0].t < cutoff:
                self._segments = [s for s in self._segments if s.t >= cutoff]
        return seg

    def window(self, seconds: int) -> str:
        """Joined text of the last `seconds` of speech."""
        now = time.monotonic() - self._start
        cutoff = now - seconds
        with self._lock:
            return " ".join(s.text for s in self._segments if s.t >= cutoff)

    def all_segments(self) -> list[dict]:
        with self._lock:
            return [asdict(s) for s in self._segments]

    def seconds_since_last(self) -> float:
        with self._lock:
            if not self._segments:
                return 1e9
            return (time.monotonic() - self._start) - self._segments[-1].t

    def count(self) -> int:
        with self._lock:
            return len(self._segments)

    def clear(self) -> None:
        with self._lock:
            self._segments = []
            self._start = time.monotonic()

    def full_text(self) -> str:
        with self._lock:
            return " ".join(s.text for s in self._segments)
