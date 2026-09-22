# Interview Copilot

Live assistant for a **disclosed, open-book** technical interview. It listens to
the call's incoming audio, keeps a rolling transcript, and on one keypress asks
Claude and Gemini — in parallel — to find the most recent real question in the
last 90 seconds and answer it in a form you can read out loud.

It does not crop audio or try to detect question boundaries in the signal. The
whole transcript window goes to the model and the model does the filtering.
That is the design: speech-to-text is unreliable at boundaries, language models
are not, so the messy job is given to the thing that is good at messy jobs.

## Disclosure

This is built for interviews where you have told the interviewer you are using
AI assistance, or where the format is explicitly open-book. Using it covertly
means the interviewer's assessment of you is wrong in a way they did not agree
to. Don't.

## How it works

```
Teams (or any call)
  └─ system output = Multi-Output Device ─┬─ your speakers   (you hear it)
                                          └─ BlackHole 2ch   (we capture it)
                                               └─ ffmpeg, 3s chunks
                                                    └─ whisper-server (large-v3-turbo, warm)
                                                         └─ rolling transcript
                                                              └─ [⌃⇧Space]
                                                                   ├─ Claude  ─┐
                                                                   └─ Gemini  ─┴─ two panes
```

Only the **remote** side of the call reaches BlackHole — your own microphone
doesn't loop back — so the transcript is mostly the interviewer, which is
exactly what you want.

## Setup

```bash
brew install blackhole-2ch whisper-cpp ffmpeg switchaudio-osx
```

Then build a Multi-Output Device once (Audio MIDI Setup → + → Create
Multi-Output Device → tick **BlackHole 2ch** and your speakers/headphones).
Full walkthrough with the gotchas: [docs/audio-setup.md](docs/audio-setup.md).

```bash
cp .env.example .env    # add ANTHROPIC_API_KEY and GEMINI_API_KEY
./scripts/preflight.sh  # must be all-green before the call
./scripts/start.sh      # http://127.0.0.1:8477
```

## Using it

1. Run `./scripts/preflight.sh`. If it isn't green, fix it before the call.
2. Set your system output **and** the Teams speaker to *Multi-Output Device*.
3. Open http://127.0.0.1:8477 on a second monitor. Click **Start capture**.
4. When you get a question, hit **⌃⇧Space** (works system-wide, you don't need
   the window focused). Read the LEAD line, then the bullets.

The button waits for the tail of the question to finish transcribing before it
answers, so pressing it the instant they stop talking is fine.

### What you're reading

```
Q: <the question, restated>

<LEAD - one or two sentences, a complete answer, sayable in 10 seconds>

- supporting detail
- supporting detail
- supporting detail

IF PUSHED: <most likely follow-up, and its answer>
```

Say the LEAD. The bullets are the specifics you'd otherwise blank on. IF PUSHED
is there because the follow-up is usually the part that actually decides it.

## Latency

Measured on this machine, warm:

| Stage | Time |
|---|---|
| Speech → transcript | ~3s chunk + 0.3s transcription |
| Gemini 2.5 Flash → first token | **815ms** |
| Gemini → complete answer | ~1.9s |
| Claude via API | comparable (needs `ANTHROPIC_API_KEY`) |
| Claude via CLI fallback | ~8.6s — too slow, get a key |

Gemini's thinking budget is set to 0 deliberately; with it on, first token took
5.3s and the answers were no better for this format.

## Configuration

Everything is in `.env` — `WINDOW_SECONDS` (default 90) is the main dial. Raise
it for long multi-part questions, lower it if the interviewer rambles and the
model keeps latching onto stale context.

## Tests

```bash
./.venv/bin/python scripts/test_filter.py   # does it ignore small talk?
./.venv/bin/python scripts/e2e.py           # full pipeline, real audio
```

`test_filter.py` is the one that matters. It feeds realistic noisy transcripts —
weekend chat, screen-share logistics, the candidate's own questions, a follow-up
probe that must beat the original question — and asserts which one gets answered.
