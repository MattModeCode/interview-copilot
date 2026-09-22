# Interview Copilot

Live assistant for a **disclosed, open-book** technical interview. It listens to
the call's incoming audio, keeps a rolling transcript, and on one keypress asks
Sonnet to find the most recent real question in the last 90 seconds and answer
it in a form you can read out loud.

**No API key.** Drives the `claude` CLI headlessly using your existing
subscription login.

A second pane was tried and dropped. Gemini CLI's free OAuth tier for
individuals is discontinued by Google. The Antigravity CLI was checked too —
every headless flag (`-p`, `--print`, `--prompt`, `--headless`, `--cli`)
returns silently; the binary is a thin launcher for the IDE app (`#!/usr/bin/env
node \ require('../')`), not an agent with a CLI mode. Both would need a
workaround, which wasn't wanted, so this is Sonnet alone.

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
                                                                        └─ claude CLI (sonnet)
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
cp .env.example .env    # defaults work as-is; no keys needed
./scripts/preflight.sh  # must be all-green before the call
./scripts/start.sh      # http://127.0.0.1:8477
```

You need to be logged in to Claude Code already (`claude` on its own, once).
`preflight.sh` proves it by running a real turn, which is also the only way to
detect that your subscription is out of quota.

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

A cold `claude -p` costs ~5s of startup, which is unusable here. Two things fix
it. First, flags: `--strict-mcp-config`, `--setting-sources ''`, `--restricted`,
and a replaced system prompt skip MCP servers, settings, plugins and CLAUDE.md
discovery — 4.98s → 2.43s. Second, and the real win, the process is never
restarted: `--input-format stream-json` holds one session open for the whole
interview, so startup is paid once when you click Start capture.

Measured on this machine, consecutive turns on a live session:

| Stage | Time |
|---|---|
| Speech → transcript | ~3s chunk + 0.3s transcription |
| Boot + warmup, per model | ~2.4s, paid **once** |
| Sonnet → first token | **630–970ms** |
| Either → complete answer | 3–5s (you read the LEAD as it streams) |

Latency stays flat as the session accumulates turns — the prompt cache is warm
and the transcript is the only thing growing.

## Quota

This burns your Claude subscription's session allowance, and **it will run out
if you are careless** — it did during development, mid-benchmark:

```
You've hit your session limit · resets 1:50pm
```

What the design does about it:

- One persistent session, not a process per answer. An earlier version
  spawned a fresh process per trigger and paid an extra warmup round trip
  every time — roughly double the quota for worse latency.
- `RECYCLE_AFTER` (default 8) is the only other thing that costs a spare turn.
  Don't lower it.
- `preflight.sh` runs a real turn, so an exhausted limit shows up *before* the
  call rather than on your first question.
- If it does happen live, the status bar says `SUBSCRIPTION LIMIT REACHED` with
  the reset time rather than failing silently.

Don't leave capture running through practice sessions you don't need, and run
preflight on the same day as the interview, not the night before.

## Why only one model

Three options were checked for a second pane, in order:

1. **Gemini CLI** — Google discontinued the free OAuth tier for individuals
   mid-2026; it errors with `IneligibleTierError` and points at Antigravity.
   Forcing API-key auth got past that and then failed on quota routing.
2. **Antigravity CLI** (`/usr/local/bin/antigravity`) — no headless mode.
   `-p`, `--print`, `--prompt`, `--headless`, `--cli` all return silently with
   no output. The binary is `#!/usr/bin/env node \ require('../')`, a launcher
   for the IDE app, not an agent.
3. Both would need a key or a workaround that wasn't wanted, so the answer is
   Sonnet alone rather than Sonnet paired with something worse.

## Domain briefing (do this before a specific interview)

Copy `domain.md.example` to `domain.md` (gitignored) and edit it for the
actual interview: the subject matter, the vocabulary in scope, and — this is
the part that matters — the specific technical terms most likely to get
mangled by speech-to-text, so the model can silently correct them before it
even looks for the question.

This exists because whisper mishears domain vocabulary as the nearest
ordinary English word: "rate equation" comes back as "race equation",
"anode/cathode" as "an ode/cathoad". Without a domain brief the model has no
way to know "race equation" doesn't belong in the sentence; with one, it
resolves it silently and answers the real question. Verified in
`scripts/test_filter.py` — a synthetic "race equation" transcript is answered
as a rate-law question on both models.

The example checked in is filled out for a ChemE Car interview (chemical
engineering + electronics: reaction kinetics, electrode reactions, control
circuits) as a template for the format. Write your own for a different field
— it's plain text, no schema.

If `domain.md` doesn't exist, the tool runs exactly as before with no domain
context. This step is optional, not required.

## Configuration

Everything is in `.env`.

| Setting | Default | What it does |
|---|---|---|
| `WINDOW_SECONDS` | 90 | How far back the trigger looks. Raise for long multi-part questions, lower if the interviewer rambles and the model latches onto stale context. |
| `CLAUDE_MODEL` | `sonnet` | Model alias passed to the CLI. |
| `RECYCLE_AFTER` | 8 | Turns before a session is replaced, so old answers stop anchoring new ones. Costs one warmup turn each time. |
| `CHUNK_SECONDS` | 3 | Transcription slice. Smaller means less tail lag and more word-splitting at boundaries. |

## Tests

```bash
./.venv/bin/python scripts/test_filter.py sonnet   # does it ignore small talk?
./.venv/bin/python scripts/e2e.py                  # full pipeline, real audio
```

`test_filter.py` is the one that matters. It feeds realistic noisy transcripts —
weekend chat, screen-share logistics, the candidate's own questions, a follow-up
probe that must beat the original question — and asserts which one gets answered.
