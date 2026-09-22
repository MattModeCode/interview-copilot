"""Does the prompt pick the right question out of a noisy transcript?

Each case is a realistic messy window. `expect` is a substring that must
appear in the restated Q: line; `reject` are things that must NOT be what
it answered.
"""
import asyncio, sys, re
sys.path.insert(0, ".")
from server.models import gemini_stream
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
        # "for session data" is legitimate context in the restatement; what
        # matters is that it answered the hashmap probe, not the original.
        expect=["hashmap", "in-memory"], reject=["how would you store"],
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


async def run_case(c):
    msg = build_user_message(c["transcript"], 90)
    out = ""
    async for d in gemini_stream(msg):
        out += d
    qline = ""
    m = re.search(r"^Q:(.*)$", out, re.M)
    if m:
        qline = m.group(1).strip().lower()
    body = out.lower()
    hit = any(e.lower() in qline or e.lower() in body[:400] for e in c["expect"])
    bad = [r for r in c["reject"] if r.lower() in qline]
    ok = hit and not bad
    print(f"{'PASS' if ok else 'FAIL'}  {c['name']}")
    print(f"      Q: {qline[:110]}")
    if not ok:
        print(f"      expected one of {c['expect']}, rejected-hits={bad}")
        print("      ---\n" + "\n".join("      " + l for l in out.splitlines()[:14]))
    return ok


async def main():
    results = [await run_case(c) for c in CASES]
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


sys.exit(asyncio.run(main()))
