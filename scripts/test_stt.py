"""Tests for the capture path: endpointing, filtering, and failure isolation.

No pytest -- this repo runs its checks as plain scripts, and these need to be
runnable ten minutes before an interview by somebody who is not thinking about
test runners.

The decoder tests use real audio from fixtures/ because the defect being
guarded against is a word lost at a cut, and no synthetic tone can show that.
The rest are pure functions over synthetic PCM and run in milliseconds.
"""
import math
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import config, vad          # noqa: E402
from server.stt import Transcriber, is_artifact  # noqa: E402

RATE = config.SAMPLE_RATE
PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  \033[32mpass\033[0m  {name}")
    else:
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m  {name}" + (f"  -- {detail}" if detail else ""))


def tone(ms: int, amp: int = 9000, hz: int = 220) -> bytes:
    n = int(RATE * ms / 1000)
    return b"".join(
        struct.pack("<h", int(amp * math.sin(2 * math.pi * hz * i / RATE)))
        for i in range(n)
    )


def silence(ms: int) -> bytes:
    return b"\x00\x00" * int(RATE * ms / 1000)


def wav_pcm(name: str) -> bytes:
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-i", str(ROOT / "fixtures" / f"{name}.wav"),
         "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
        capture_output=True, check=True,
    ).stdout


# --- the segmenter ------------------------------------------------------
def test_segmenter() -> None:
    print("\nsegmenter")

    s = vad.Segmenter()
    out = s.feed(silence(3000))
    check("silence alone produces no segment", out == [])

    s = vad.Segmenter()
    out = s.feed(silence(400) + tone(1200) + silence(1500))
    check("one utterance between pauses -> one segment", len(out) == 1,
          f"got {len(out)}")

    s = vad.Segmenter()
    out = s.feed(silence(300) + tone(900) + silence(1200) + tone(900) + silence(1200))
    check("two utterances -> two segments", len(out) == 2, f"got {len(out)}")

    s = vad.Segmenter()
    out = s.feed(silence(300) + tone(900) + silence(200) + tone(900) + silence(1200))
    check("a short breath does not split an utterance", len(out) == 1,
          f"got {len(out)}")

    s = vad.Segmenter()
    out = s.feed(silence(300) + tone(60) + silence(1200))
    check("a click shorter than MIN_SPEECH_MS is dropped", out == [],
          f"got {len(out)}")

    s = vad.Segmenter()
    out = s.feed(silence(300) + tone(1000))
    check("an unfinished utterance is not emitted early", out == [])
    tail = s.flush()
    check("flush() emits the unfinished utterance", tail is not None)

    # The preroll: the segment must begin before the detector fired.
    s = vad.Segmenter()
    out = s.feed(silence(1000) + tone(1500) + silence(1200))
    check("the segment carries preroll ahead of speech",
          out and vad.duration(out[0]) > 1.5,
          f"{vad.duration(out[0]):.2f}s for 1.5s of tone" if out else "no segment")


def test_forced_cut() -> None:
    print("\nforced cut (a speaker who does not pause)")
    # Continuous tone past SEGMENT_MAX_SECONDS, with a real word gap in it.
    long = tone(int(config.SEGMENT_MAX_SECONDS * 1000) - 400)
    s = vad.Segmenter()
    out = s.feed(silence(300) + long + silence(120) + tone(3000) + silence(1200))
    check("a run past the max is cut into pieces", len(out) >= 2, f"got {len(out)}")
    if out:
        joined = sum(vad.duration(o) for o in out) + vad.duration(s.flush() or b"")
        expected = vad.duration(long + tone(3000)) 
        check("no audio is lost across the cut", joined >= expected,
              f"{joined:.2f}s kept of {expected:.2f}s spoken")

    # No gap at all within the search window: the cut must be deferred to the
    # hard ceiling rather than landing inside continuous speech.
    s = vad.Segmenter()
    span = config.SEGMENT_MAX_SECONDS + config.SEGMENT_GRACE_SECONDS / 2
    out = s.feed(silence(300) + tone(int(span * 1000)))
    check("a gapless run is held past the soft max", out == [], f"got {len(out)}")


# --- the artifact filter ------------------------------------------------
def test_filter() -> None:
    print("\nartifact filter")
    for word in ("Okay.", "So.", "Bye.", "Right, that makes sense.", "Mm hm.",
                 "Yeah.", "Sure."):
        check(f"real speech survives: {word!r}", not is_artifact(word))
    for junk in ("Thanks for watching!", "[BLANK_AUDIO]", "(silence)",
                 "...", "♪", "Subtitles by the amara.org community"):
        check(f"artifact rejected: {junk!r}", is_artifact(junk))


# --- the decoder --------------------------------------------------------
def test_decoder() -> None:
    print("\ndecoder (real audio -- loads the model, takes a minute)")
    stt = Transcriber()
    stt.start()

    text = " ".join(stt.transcribe(wav_pcm("rate_limiter"))).lower()
    check("the word at the old three-second cut survives",
          "rate limiter" in text, text[:120])
    check("'per second' is not split into 'per session. second'",
          "per second" in text and "session" not in text, text[:120])

    text = " ".join(stt.transcribe(wav_pcm("domain_terms"))).lower()
    check("domain homophone stays 'rate equation'",
          "rate equation" in text and "race equation" not in text, text[:120])
    check("the word after a pause is not eaten",
          "whether the reaction is" in text, text[:160])

    short = " ".join(stt.transcribe(wav_pcm("short_utterances"))).lower()
    check("short real words are no longer blacklisted away",
          "okay" in short and "got it" in short, short[:120])

    check("pure silence transcribes to nothing",
          stt.transcribe(silence(4000)) == [])

    # A decode that raises must be contained: one bad utterance cannot end a
    # session, because the session is a live interview.
    broken = Transcriber()
    broken.start()

    class Boom:
        def transcribe(self, *a, **k):
            raise RuntimeError("synthetic decode failure")

    broken._model = Boom()
    out = broken.feed(silence(300) + tone(1200) + silence(1200))
    check("a decode failure is contained, not raised", out == [])
    check("a decode failure is reported on .error",
          (broken.error or "").startswith("stt:"), str(broken.error))


# --- the decoder prompt ------------------------------------------------
def test_prompt() -> None:
    print("\nprompt budget")
    from server.config import _vocab_prompt

    terms = _vocab_prompt(
        "- **rate** vs **race**: the homophone pair\n"
        "- anode: the electrode\n"
        "Use `stoichiometry` correctly. The PID loop matters.\n"
    )
    for want in ("rate", "race", "anode", "stoichiometry", "PID"):
        check(f"domain.md term extracted: {want}", want in terms, terms)
    check("emphasis inside a glossary line is not captured garbled",
          "**" not in terms and " vs " not in terms, terms)

    stt = Transcriber()
    stt._context = "small talk " * 200          # far past the prompt budget
    prompt = stt._prompt()
    check("the prompt stays inside the budget",
          len(prompt) <= config.STT_PROMPT_CHARS, f"{len(prompt)} chars")
    if config.STT_PROMPT:
        check("a long preamble does not evict the domain vocabulary",
              prompt.startswith(config.STT_PROMPT[:40]), prompt[:80])
    check("recent speech is still carried", "small talk" in prompt)


def test_trigger_flush() -> None:
    print("\ntrigger flush (question not finished yet)")
    stt = Transcriber()
    stt.start()
    # Speech with no closing pause: nothing may be emitted by feed alone.
    pcm = wav_pcm("rate_limiter")
    mid = len(pcm) // 2 // 2 * 2
    emitted = stt.feed(pcm[:mid])
    check("an open utterance is not emitted early", emitted == [],
          " | ".join(emitted))
    flushed = " ".join(stt.flush())
    check("the trigger's flush returns the words already spoken",
          "rate limiter" in flushed.lower(), flushed[:120])


def main() -> int:
    if not any((ROOT / "fixtures").glob("*.wav")):
        sys.exit("no fixtures -- run scripts/make_fixtures.py first")
    test_segmenter()
    test_forced_cut()
    test_filter()
    test_prompt()
    test_decoder()
    test_trigger_flush()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


sys.exit(main())
