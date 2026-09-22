"""Speech to text: faster-whisper, in-process, with context that carries.

Three things the old whisper-server path did not do, each of which cost
accuracy on its own:

1. **Context.** Every three-second chunk was decoded in total isolation, so
   the model re-guessed every proper noun and every domain term from scratch,
   several times a minute. Here the tail of the running transcript is fed
   forward as the prompt, and `condition_on_previous_text` links the segments
   inside one decode.

2. **The domain brief.** `domain.md` exists to name the terms that get
   mis-transcribed, and it was only ever shown to the answering model -- after
   the damage. It now primes the decoder, where it can prevent the homophone
   instead of correcting it downstream.

3. **A real confidence signal.** Hallucinations were caught with a blacklist
   of words ("okay", "so", "bye", "you") that are also ordinary interview
   speech, so genuine short answers were deleted. Whisper reports
   `no_speech_prob` and `avg_logprob`; those are the right instrument, and the
   blacklist shrinks to the handful of phrases that are pure artifact.
"""
import re
import threading

from . import config, vad

# Whisper emits these over silence and music. Unlike "okay" or "so", none of
# them is a thing an interviewer says, so a literal match is safe.
_ARTIFACTS = {
    "thanks for watching", "thanks for watching!", "thank you for watching",
    "please subscribe", "subtitles by the amara.org community",
    "subtitles by the amara.org community.", "[blank_audio]", "(silence)",
    "[music]", "(upbeat music)", "[ silence ]", "you", "♪", "♪♪",
}


def is_artifact(text: str) -> bool:
    t = text.strip().lower()
    if t.rstrip(".!?") in {a.rstrip(".!?") for a in _ARTIFACTS}:
        return True
    if not re.search(r"[a-z0-9]", t):
        return True
    # A lone bracketed tag: [BLANK_AUDIO], (wind blowing).
    return bool(re.fullmatch(r"[\[\(][^\]\)]*[\]\)][.!?]?", t))


class Transcriber:
    """Owns the model, the segmenter, and the rolling decode context."""

    def __init__(self) -> None:
        self._model = None
        self._seg = vad.Segmenter()
        self._seg_lock = threading.Lock()
        self._lock = threading.Lock()
        self._context = ""
        self.error: str | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Load the model. Slow the first time (it downloads); warm after."""
        if self._model is not None:
            return
        try:
            from faster_whisper import WhisperModel
        except ImportError as err:
            raise RuntimeError(
                f"faster-whisper is not installed ({err}) -- "
                "pip install -r requirements.txt"
            ) from err
        self._model = WhisperModel(
            config.STT_MODEL,
            device=config.STT_DEVICE,
            compute_type=config.STT_COMPUTE,
            cpu_threads=config.STT_THREADS,
            download_root=config.STT_CACHE or None,
        )

    def stop(self) -> None:
        self._model = None
        self._seg = vad.Segmenter()
        self._context = ""

    def reset_context(self) -> None:
        """Drop the carried context, e.g. when the transcript is cleared."""
        self._context = ""

    # -- the stream --------------------------------------------------------
    def feed(self, pcm: bytes) -> list[str]:
        """PCM in, finished utterances out. Empty list is the normal case."""
        with self._seg_lock:
            ready = self._seg.feed(pcm)
        return [t for seg in ready for t in self._decode(seg)]

    def flush(self) -> list[str]:
        """Close and decode the open utterance, now.

        Called when capture stops, and when the operator hits the trigger
        mid-sentence -- the segmenter is waiting for a pause that may be
        fifteen seconds away, and the words already spoken are the question.
        """
        with self._seg_lock:
            tail = self._seg.flush()
        return self._decode(tail) if tail else []

    def transcribe(self, pcm: bytes) -> list[str]:
        """Whole-buffer convenience, used by the WER harness and tests."""
        return self.feed(pcm) + self.flush()

    # -- decoding ----------------------------------------------------------
    def _prompt(self) -> str:
        """Domain vocabulary first, then as much recent transcript as fits.

        Both are truncated from the *left* of the transcript, never the left
        of the vocabulary: the primer is the thing that stops "rate equation"
        becoming "race equation", and a long preamble of small talk must not
        be what pushes it out of the window.
        """
        vocab = config.STT_PROMPT
        room = config.STT_PROMPT_CHARS - len(vocab) - 1
        if room <= 0:
            return vocab[: config.STT_PROMPT_CHARS]
        tail = self._context[-room:] if self._context else ""
        return " ".join(p for p in (vocab, tail) if p)

    def _decode(self, pcm: bytes) -> list[str]:
        if self._model is None:
            self.start()
        if vad.duration(pcm) < config.MIN_SPEECH_MS / 1000:
            return []
        try:
            with self._lock:
                segments, _info = self._model.transcribe(
                    vad.to_float32(pcm),
                    language=config.STT_LANGUAGE,
                    beam_size=config.STT_BEAM,
                    # The temperature ladder: if a decode looks degenerate by
                    # the thresholds below, retry it hotter instead of
                    # shipping the garbage, which temperature=0 alone did.
                    temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
                    compression_ratio_threshold=2.4,
                    log_prob_threshold=config.STT_LOGPROB_MIN,
                    no_speech_threshold=config.STT_NO_SPEECH_MAX,
                    condition_on_previous_text=True,
                    initial_prompt=self._prompt() or None,
                    vad_filter=config.STT_VAD_FILTER,
                    word_timestamps=False,
                )
                out: list[str] = []
                for s in segments:
                    text = (s.text or "").strip()
                    if not text or is_artifact(text):
                        continue
                    if s.no_speech_prob > config.STT_NO_SPEECH_MAX:
                        continue
                    if s.avg_logprob < config.STT_LOGPROB_MIN:
                        continue
                    out.append(text)
        except Exception as err:  # one bad segment must never kill the stream
            self.error = f"stt: {err}"
            return []

        if out:
            joined = " ".join(out)
            self._context = (self._context + " " + joined).strip()[
                -config.STT_PROMPT_CHARS:
            ]
        return out
