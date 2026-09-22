"""Build the STT fixture set: synthetic interview speech with known references.

`say` is the source because it is repeatable. A recorded interview would be
more realistic but it cannot be regenerated on another machine, and a WER
number you cannot reproduce is not a baseline -- it is an anecdote.

Two voices and two speaking rates, because the boundary defect this harness
exists to catch depends on where words land in time: a word straddling a
segment cut is destroyed, and which word that is moves with the rate.
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "fixtures"

# (name, voice, words-per-minute, reference text)
CASES = [
    (
        "rate_limiter", "Samantha", 180,
        "So walk me through how you would design a distributed rate limiter "
        "for an API handling ten thousand requests per second. I am interested "
        "in where you would keep the counters and what happens when a node "
        "falls out of the cluster mid window."
    ),
    (
        "smalltalk_then_question", "Alex", 175,
        "Hey good morning how was your weekend. Oh nice the weather has been "
        "unbelievable lately. Cool let me share my screen real quick can you "
        "see that okay. Great so tell me about a time you disagreed with a "
        "technical decision and what you did about it."
    ),
    (
        "domain_terms", "Samantha", 190,
        "For the propulsion reaction can you walk me through the rate equation "
        "you are using and how the rate constant depends on temperature. I also "
        "want to hear about the stoichiometry and whether the reaction is "
        "first order in the limiting reagent."
    ),
    (
        "short_utterances", "Alex", 165,
        "Okay. So. Right that makes sense. But why not just use a hashmap in "
        "memory instead. What breaks there. Mm hm. Got it. Yeah okay so what "
        "would you change if the traffic doubled overnight."
    ),
    (
        "long_unbroken", "Samantha", 195,
        "The system currently writes every event to Postgres synchronously on "
        "the request path and that is fine at our volume today but we expect "
        "roughly forty times the traffic after the migration so I want to know "
        "what you would move off the hot path first and how you would measure "
        "whether it actually helped rather than just feeling faster."
    ),
]


def main() -> int:
    if not shutil.which("say") or not shutil.which("ffmpeg"):
        sys.exit("needs `say` (macOS) and ffmpeg on PATH")
    OUT.mkdir(exist_ok=True)
    for name, voice, rate, text in CASES:
        wav = OUT / f"{name}.wav"
        ref = OUT / f"{name}.txt"
        ref.write_text(text.strip() + "\n")
        raw = OUT / f"{name}.raw.aiff"
        subprocess.run(
            ["say", "-v", voice, "-r", str(rate), "-o", str(raw), text],
            check=True,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(raw), "-ac", "1", "-ar", "16000", str(wav)],
            check=True,
        )
        raw.unlink(missing_ok=True)
        dur = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(wav)],
            capture_output=True, text=True,
        ).stdout.strip()
        print(f"  {name:26s} {float(dur):5.1f}s  {len(text.split()):3d} words")
    print(f"\n{len(CASES)} fixtures in {OUT}")
    return 0


sys.exit(main())
