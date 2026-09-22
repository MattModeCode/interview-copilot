"""End-to-end: speak into BlackHole, confirm it comes out as an answer."""
import asyncio, json, subprocess, sys, time
import websockets

URL = "ws://127.0.0.1:8477/ws"

SCRIPT = [
    "Hey, good morning. How was your weekend?",
    "Oh nice. Yeah, the weather has been unbelievable lately.",
    "Cool. Let me share my screen real quick. Can you see that okay?",
    "Great. So, walk me through how you would design a distributed rate "
    "limiter for an API handling ten thousand requests per second.",
]


async def main():
    async with websockets.connect(URL) as ws:
        hello = json.loads(await ws.recv())
        print(f"connected. panes={hello['config']['panes']}")

        await ws.send(json.dumps({"type": "clear"}))
        await ws.send(json.dumps({"type": "start"}))

        # wait for capture to confirm
        deadline = time.time() + 90
        while time.time() < deadline:
            m = json.loads(await ws.recv())
            if m.get("type") == "status":
                if not m.get("capturing"):
                    print(f"FAIL capture did not start: {m.get('error')}")
                    return 1
                print("capture running")
                break

        # speak the script through the current output device
        for line in SCRIPT:
            subprocess.run(["say", "-v", "Samantha", "-r", "180", line], check=True)
            await asyncio.sleep(0.4)
        print("finished speaking; waiting for transcription + model warmup")

        segs = []
        settle = time.time() + 30
        while time.time() < settle:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            except asyncio.TimeoutError:
                continue
            if m.get("type") == "segment":
                segs.append(m["text"])
                print(f"  [stt] {m['text'][:90]}")
            elif m.get("type") == "pools":
                pools = m["pools"]
                if pools:
                    print("  [warm] " + ", ".join(
                        f"{k}={v['error'] or str(v['ready'])+' ready'}"
                        for k, v in pools.items()))

        if not segs:
            print("FAIL nothing was transcribed -- audio never reached BlackHole")
            return 1

        print("\ntriggering answer...")
        await ws.send(json.dumps({"type": "trigger", "window": 120}))

        out = {"a": "", "b": ""}
        meta = {}
        done = set()
        deadline = time.time() + 90
        while time.time() < deadline and len(done) < 2:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                continue
            if m.get("type") != "answer":
                continue
            p, ev = m["pane"], m["event"]
            if ev == "delta":
                out[p] += m["text"]
            elif ev == "first_token":
                meta[p] = m["ms"]
                print(f"  {p}: first token in {m['ms']}ms")
            elif ev == "done":
                done.add(p)
            elif ev == "error":
                print(f"  {p} ERROR: {m['message']}")
                done.add(p)

        await ws.send(json.dumps({"type": "stop"}))

        ok = True
        for p in ("a", "b"):
            text = out[p].strip()
            print(f"\n===== {p.upper()} =====\n{text[:700]}")
            if not text:
                print(f"  ({p} produced nothing)")
                ok = False
                continue
            low = text.lower()
            if "rate limit" not in low:
                print(f"  FAIL {p} did not answer the rate limiter question")
                ok = False
            if "weekend" in low.split("if pushed")[0][:200]:
                print(f"  FAIL {p} leaked small talk into the answer")
                ok = False
        print("\n" + ("E2E PASS" if ok else "E2E FAIL"))
        return 0 if ok else 1


sys.exit(asyncio.run(main()))
