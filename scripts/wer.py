"""Word error rate for the STT backend, measured on the fixture set.

Two backends. `legacy` is a frozen copy of the whisper.cpp pipeline as it
shipped -- ffmpeg cutting hard three-second WAVs, each POSTed to whisper-server
with no shared context, then a word blacklist. It is reproduced here rather
than imported so the baseline survives the rewrite that replaces it.
`faster` is the current server.stt path.

WER alone hides the failure that actually matters here, so deletions are
reported separately: a mangled word is a nuisance, a word that never appears
is a question the copilot answers wrong.

    python scripts/wer.py                  # current backend
    python scripts/wer.py --backend legacy # the frozen whisper.cpp baseline
"""
import argparse
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "fixtures"

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven",
         "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
         "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety"]


def _spell(n: int) -> str:
    """Numerals to words, so '10,000' is not scored as wrong for 'ten thousand'."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        return (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else "")).strip()
    for div, word in ((1_000_000, "million"), (1000, "thousand"), (100, "hundred")):
        if n >= div:
            head = _spell(n // div) + " " + word
            rest = n % div
            return head + (" " + _spell(rest) if rest else "")
    return str(n)


def normalize(text: str) -> list[str]:
    t = text.lower().replace("-", " ")
    t = re.sub(r"(\d),(\d)", r"\1\2", t)
    t = re.sub(r"\d+", lambda m: " " + _spell(int(m.group())) + " ", t)
    t = re.sub(r"[^a-z' ]+", " ", t)
    return t.split()


def align(ref: list[str], hyp: list[str]) -> tuple[int, int, int]:
    """Levenshtein over words -> (substitutions, deletions, insertions)."""
    n, m = len(ref), len(hyp)
    # (cost, sub, del, ins)
    prev = [(j, 0, 0, j) for j in range(m + 1)]
    for i in range(1, n + 1):
        cur = [(i, 0, i, 0)] + [(0, 0, 0, 0)] * m
        for j in range(1, m + 1):
            if ref[i - 1] == hyp[j - 1]:
                cur[j] = prev[j - 1]
                continue
            c_sub = (prev[j - 1][0] + 1, prev[j - 1][1] + 1, prev[j - 1][2], prev[j - 1][3])
            c_del = (prev[j][0] + 1, prev[j][1], prev[j][2] + 1, prev[j][3])
            c_ins = (cur[j - 1][0] + 1, cur[j - 1][1], cur[j - 1][2], cur[j - 1][3] + 1)
            cur[j] = min(c_sub, c_del, c_ins)
        prev = cur
    _, sub, dele, ins = prev[m]
    return sub, dele, ins


# --- backends -----------------------------------------------------------
_LEGACY_NOISE = {
    "", "you", "thank you", "thanks for watching", "thank you.", "you.",
    "thanks for watching!", "please subscribe", "[blank_audio]", "(silence)",
    "bye", "bye.", "subtitles by the amara.org community", "so", "so.",
    "[music]", "(upbeat music)", "[ silence ]", ".", "...", "1", "okay",
}


def legacy(wav: Path) -> str:
    """The shipped whisper.cpp path: hard 3s cuts, no shared context."""
    import httpx

    server = "http://127.0.0.1:8178"
    out: list[str] = []
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav),
             "-ac", "1", "-ar", "16000", "-f", "segment", "-segment_time", "3",
             "-reset_timestamps", "1", f"{d}/chunk_%06d.wav"],
            check=True,
        )
        for chunk in sorted(Path(d).glob("chunk_*.wav")):
            if chunk.stat().st_size < 4000:
                continue
            with open(chunk, "rb") as fh:
                r = httpx.post(
                    server + "/inference",
                    files={"file": (chunk.name, fh, "audio/wav")},
                    data={"response_format": "json", "temperature": "0"},
                    timeout=120.0,
                )
            r.raise_for_status()
            text = (r.json().get("text") or "").strip()
            low = text.lower()
            if low in _LEGACY_NOISE or not re.search(r"[a-z]", low):
                continue
            if re.fullmatch(r"[\[\(].*[\]\)]", low):
                continue
            out.append(text)
    return " ".join(out)


def faster(wav: Path) -> str:
    from server.stt import Transcriber
    from server import config

    stt = Transcriber()
    stt.start()
    try:
        pcm = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(wav),
             "-ac", "1", "-ar", str(config.SAMPLE_RATE), "-f", "s16le", "-"],
            capture_output=True, check=True,
        ).stdout
        return " ".join(stt.transcribe(pcm))
    finally:
        stt.stop()


BACKENDS = {"legacy": legacy, "faster": faster}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=sorted(BACKENDS), default="faster")
    ap.add_argument("--show", action="store_true", help="print each transcript")
    args = ap.parse_args()

    wavs = sorted(FIXTURES.glob("*.wav"))
    if not wavs:
        sys.exit("no fixtures -- run scripts/make_fixtures.py first")

    run = BACKENDS[args.backend]
    tot_ref = tot_sub = tot_del = tot_ins = 0
    tot_audio = tot_wall = 0.0
    print(f"backend: {args.backend}\n")
    print(f"  {'fixture':26s} {'WER':>7s} {'drop':>5s} {'sub':>4s} {'ins':>4s} {'xRT':>6s}")
    for wav in wavs:
        ref = normalize((wav.with_suffix(".txt")).read_text())
        t0 = time.monotonic()
        hyp_text = run(wav)
        wall = time.monotonic() - t0
        hyp = normalize(hyp_text)
        sub, dele, ins = align(ref, hyp)
        wer = (sub + dele + ins) / max(1, len(ref))
        audio = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(wav)], capture_output=True, text=True).stdout)
        print(f"  {wav.stem:26s} {wer:6.1%} {dele:5d} {sub:4d} {ins:4d} {wall / audio:5.2f}x")
        if args.show:
            print(f"      {hyp_text}")
        tot_ref += len(ref); tot_sub += sub; tot_del += dele; tot_ins += ins
        tot_audio += audio; tot_wall += wall

    wer = (tot_sub + tot_del + tot_ins) / max(1, tot_ref)
    print(f"\n  {'TOTAL':26s} {wer:6.1%} {tot_del:5d} {tot_sub:4d} {tot_ins:4d} "
          f"{tot_wall / tot_audio:5.2f}x")
    print(f"\n  {tot_ref} reference words, {tot_audio:.0f}s audio, "
          f"{tot_wall:.0f}s wall")
    return 0


sys.exit(main())
