import asyncio
import contextlib
import json
import threading
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from . import config
from .capture import Capture
from .models import answer, pool_status, start_pools, stop_pools
from .transcript import Transcript

UI = Path(__file__).resolve().parent.parent / "ui"

app = FastAPI(title="interview-copilot")
transcript = Transcript()
clients: set[WebSocket] = set()
_loop: asyncio.AbstractEventLoop | None = None
_answer_task: asyncio.Task | None = None


# --- fan-out ------------------------------------------------------------
async def broadcast(payload: dict) -> None:
    dead = []
    for ws in list(clients):
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


def _on_segment(seg) -> None:
    """Called from the capture thread -- hop back onto the event loop."""
    if _loop is None:
        return
    asyncio.run_coroutine_threadsafe(
        broadcast({"type": "segment", "t": seg.t, "text": seg.text, "wall": seg.wall}),
        _loop,
    )


capture = Capture(transcript, on_segment=_on_segment)


# --- the trigger --------------------------------------------------------
async def do_trigger(window: int | None = None) -> None:
    global _answer_task
    if _answer_task and not _answer_task.done():
        _answer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _answer_task

    w = window or config.WINDOW_SECONDS

    # Tail flush. Audio is cut where the speaker pauses, and an interviewer
    # who runs straight on can hold the end of the question inside an open
    # segment for up to SEGMENT_MAX_SECONDS -- which is exactly when the
    # question ENDS. So the trigger does not wait out a guessed delay, as it
    # had to when chunks were on a timer. It closes the segment itself and
    # decodes it, which costs only the decode.
    if capture.running and transcript.seconds_since_last() > 1.2:
        await broadcast({"type": "flushing"})
        await asyncio.to_thread(capture.flush_now)

    text = transcript.window(w)
    await broadcast({"type": "answer_start", "window": w, "transcript": text})

    async def emit(pane: str, event: str, data: dict) -> None:
        await broadcast({"type": "answer", "pane": pane, "event": event, **data})

    async def run() -> None:
        await answer(text, w, emit)
        # Auto-clear: each answer is a complete read on its own window, and
        # leftover text just risks the NEXT question latching onto stale
        # context (small talk, a resolved question) instead of what's
        # actually being asked now. Clearing after every answer means each
        # trigger starts from a clean slate of only what's been said since.
        transcript.clear()
        capture.stt.reset_context()
        await broadcast({"type": "cleared"})
        await broadcast({"type": "answer_end"})

    _answer_task = asyncio.create_task(run())


def trigger_from_thread() -> None:
    if _loop is not None:
        asyncio.run_coroutine_threadsafe(do_trigger(), _loop)


# --- global hotkey ------------------------------------------------------
def _start_hotkey() -> None:
    """Ctrl+Shift+Space anywhere on the system fires the trigger.

    Needs Accessibility permission (System Settings > Privacy & Security >
    Accessibility) for whatever terminal launched this. Without it pynput
    silently receives nothing, so failure here is logged, not fatal -- the
    on-screen button always works.
    """
    try:
        from pynput import keyboard
    except Exception as e:
        print(f"[hotkey] unavailable: {e}")
        return

    def on_activate() -> None:
        print("[hotkey] triggered")
        trigger_from_thread()

    try:
        hk = keyboard.GlobalHotKeys({"<ctrl>+<shift>+<space>": on_activate})
        hk.daemon = True
        hk.start()
        print("[hotkey] listening for ctrl+shift+space")
    except Exception as e:
        print(f"[hotkey] failed to bind: {e}")


# --- routes -------------------------------------------------------------
@app.on_event("startup")
async def _startup() -> None:
    global _loop
    _loop = asyncio.get_running_loop()
    if config.HOTKEY_ENABLED:
        threading.Thread(target=_start_hotkey, daemon=True).start()


async def _watch_pools() -> None:
    """Report warm-session readiness to the UI until every session is up."""
    for _ in range(200):
        st = pool_status()
        await broadcast({"type": "pools", "pools": st})
        if st and all(v["ready"] or v["error"] for v in st.values()):
            return
        await asyncio.sleep(1.0)


@app.on_event("shutdown")
async def _shutdown() -> None:
    capture.stop()
    stop_pools()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(UI / "index.html")


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {
            "capturing": capture.running,
            "capture_error": capture.error,
            "segments": len(transcript.all_segments()),
            "model": config.CLAUDE_MODEL,
            "pool": pool_status(),
            "window_seconds": config.WINDOW_SECONDS,
            "audio_device": config.AUDIO_DEVICE,
        }
    )


@app.post("/trigger")
async def http_trigger() -> JSONResponse:
    await do_trigger()
    return JSONResponse({"ok": True})


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    clients.add(websocket)
    await websocket.send_text(
        json.dumps(
            {
                "type": "hello",
                "capturing": capture.running,
                "segments": transcript.all_segments(),
                "config": {
                    "window": config.WINDOW_SECONDS,
                    "model": config.CLAUDE_MODEL,
                    "recycle_after": config.RECYCLE_AFTER,
                },
            }
        )
    )
    try:
        while True:
            msg = json.loads(await websocket.receive_text())
            kind = msg.get("type")
            if kind == "trigger":
                await do_trigger(msg.get("window"))
            elif kind == "start":
                try:
                    await asyncio.to_thread(capture.start)
                    # Boot the warm model processes now, not on first trigger.
                    await asyncio.to_thread(start_pools)
                    await broadcast({"type": "status", "capturing": True})
                    asyncio.create_task(_watch_pools())
                except Exception as e:
                    await broadcast({"type": "status", "capturing": False, "error": str(e)})
            elif kind == "stop":
                await asyncio.to_thread(capture.stop)
                await asyncio.to_thread(stop_pools)
                await broadcast({"type": "status", "capturing": False})
            elif kind == "clear":
                transcript.clear()
                capture.stt.reset_context()
                await broadcast({"type": "cleared"})
            elif kind == "ping":
                await websocket.send_text(
                    json.dumps({"type": "pong", "error": capture.error,
                                "capturing": capture.running})
                )
    except (WebSocketDisconnect, RuntimeError):
        # RuntimeError covers an unclean close (tab killed, network drop)
        # where starlette raises instead of a clean WebSocketDisconnect.
        pass
    finally:
        clients.discard(websocket)
