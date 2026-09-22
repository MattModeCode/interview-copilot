"""Does the prompt pick the right question out of a noisy transcript?

Runs against whichever backend .env selects (api or cli), so it tests the
shipping path rather than a stand-in. Costs one real model turn per case.
"""
import asyncio
import re
import sys
sys.path.insert(0, ".")
from server import config
from server.models import make_session
from server.prompt import build_user_message

CASES = [
    dict(
        name="small talk then real question",
        transcript=(
            "yeah no problem happy to be here how was your weekend "
            "oh nice yeah the weather's been crazy honestly "
            "cool so um let me share my screen real quick can you see that "
            "yep great okay so walk me through how you'd design a rate limiter "
            "for an API that gets about ten thousand requests a second"
        ),
        expect=["rate limiter"], reject=["weekend", "weather", "screen"],
    ),
    dict(
        name="only small talk -> must refuse",
        transcript=(
            "hey good morning how's it going good good "
            "did you have any trouble finding the link no it was fine "
            "awesome we've got about forty five minutes today "
            "I'm a staff engineer on the payments team been here four years"
        ),
        expect=["no technical question"], reject=["payments team"],
    ),
    dict(
        name="follow-up probe, not the original",
        transcript=(
            "so how would you store the session data "
            "right I'd use redis with a ttl "
            "mm hm okay that makes sense "
            "but why not just use a hashmap in memory instead what breaks there"
        ),
        expect=["hashmap", "in-memory", "in memory"], reject=["how would you store"],
    ),
    dict(
        name="STT homophone: race equation -> rate equation (ChemE Car domain)",
        transcript=(
            "cool yeah nice to meet you too so um for the propulsion reaction "
            "can you walk me through the race equation you're using and how "
            "the rate constant depends on temperature"
        ),
        expect=["rate", "temperature"], reject=["race car", "racing"],
    ),
    dict(
        name="candidate's own question is ignored",
        transcript=(
            "before we start do you mind if I ask what the team size is "
            "sure it's about eight people "
            "got it thanks "
            "okay so tell me about a time you disagreed with a technical decision"
        ),
        expect=["disagree"], reject=["team size"],
    ),
]


async def run_case(c) -> bool:
    sess = make_session()
    await asyncio.to_thread(sess.start)
    if sess.failed:
        print(f"SKIP  {c['name']}: warmup failed: {sess.failed}")
        sess.close()
        return False
    out = ""
    try:
        async for d in sess.ask(build_user_message(c["transcript"], 90)):
            out += d
    finally:
        sess.close()

    m = re.search(r"^Q:(.*)$", out, re.M)
    qline = m.group(1).strip().lower() if m else ""
    hit = any(e.lower() in qline or e.lower() in out.lower()[:400] for e in c["expect"])
    bad = [r for r in c["reject"] if r.lower() in qline]
    ok = hit and not bad
    print(f"{'PASS' if ok else 'FAIL'}  {c['name']}")
    print(f"      Q: {qline[:110]}")
    if not ok:
        print(f"      expected one of {c['expect']}, rejected-hits={bad}")
        print("\n".join("      " + l for l in out.splitlines()[:14]))
    return ok


async def main() -> int:
    print(f"backend: {config.BACKEND} ({config.model_label()})\n")
    results = await asyncio.gather(*(run_case(c) for c in CASES))
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


sys.exit(asyncio.run(main()))
