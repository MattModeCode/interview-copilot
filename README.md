# Interview Copilot

Live assistant for a **disclosed, open-book** technical interview. It listens to
the call's incoming audio, keeps a rolling transcript, and on one keypress asks
Sonnet to find the most recent real question in the last 90 seconds and answer
it in a form you can read out loud.

**No API key.** Drives the `claude` CLI headlessly using your existing
subscription login.

**Each answer clears the transcript.** The moment an answer finishes
streaming, the rolling buffer empties. The next trigger only sees speech said
since that point — nothing lingers to anchor a later, unrelated question. If
you want to re-answer without new speech, that's what `Clear` does NOT do —
clearing is now automatic and continuous, one clean window per question.

A second pane (Haiku, then Gemini, then the Antigravity CLI) was tried and
dropped each time — this ships as Sonnet alone. Antigravity in particular has
no headless mode at all: every flag (`-p`, `--print`, `--prompt`, `--headless`,
`--cli`) returns silently, and the binary is a thin launcher for the IDE app
(`#!/usr/bin/env node \ require('../')`), not an agent.

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
                                               └─ ffmpeg, continuous PCM
                                                    └─ cut at pauses (server/vad.py)
                                                         └─ faster-whisper, in process
                                                         └─ rolling transcript
                                                              └─ [⌃⇧Space]
                                                                        └─ claude CLI (sonnet)
```

Only the **remote** side of the call reaches BlackHole — your own microphone
doesn't loop back — so the transcript is mostly the interviewer, which is
exactly what you want.

## Setup

```bash
brew install blackhole-2ch ffmpeg switchaudio-osx
```

Then build a Multi-Output Device once (Audio MIDI Setup → + → Create
Multi-Output Device → tick **BlackHole 2ch** and your speakers/headphones).
Full walkthrough with the gotchas: [docs/audio-setup.md](docs/audio-setup.md).

```bash
./.venv/bin/pip install -r requirements.txt
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
| Speech → transcript | pause (0.6s) + decode (~0.3x realtime) |
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

## Auto-clear

The transcript buffer wipes itself right after each answer finishes — not on
a timer, on the `answer_end` event. This means the window the NEXT trigger
sees starts from zero, built only from what's said after the last question
was answered. It stops two failure modes: an old, already-answered question
lingering in the window and getting re-answered by accident, and small talk
between questions padding out the window the model has to search through.

The manual `Clear` button still exists for wiping mid-question (e.g. the
interviewer restarts a question) — auto-clear only fires after a completed
answer, never mid-stream.

## Domain briefing (do this before a specific interview)

Copy `domain.md.example` to `domain.md` (gitignored) and edit it for the
actual interview: the subject matter, the vocabulary in scope, and — this is
the part that matters — the specific technical terms most likely to get
mangled by speech-to-text, so the model can silently correct them before it
even looks for the question.

This exists because whisper mishears domain vocabulary as the nearest
ordinary English word: "rate equation" comes back as "race equation",
"anode/cathode" as "an ode/cathoad".

The brief is used **twice**. Its jargon — anything bolded, backticked,
capitalised, or written as a glossary entry — is extracted into the decoder's
`initial_prompt`, so Whisper is primed to hear the term correctly in the first
place. The full brief still goes to the answering model, which resolves
whatever slipped through. Prevention first, correction second. Verified in
`scripts/test_stt.py` (the audio comes back as "rate equation") and
`scripts/test_filter.py` (a transcript that says "race equation" is still
answered as a rate-law question).

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
| `STT_MODEL` | `large-v3-turbo` | faster-whisper model id. `medium.en` or `small.en` if the machine is busy; expect more homophone errors. |
| `STT_COMPUTE` | `int8` | CTranslate2 quantisation. `int8_float32` is slightly more accurate and slower. |
| `SILENCE_HOLD_MS` | 600 | Pause that ends an utterance. Lower means less tail lag and more mid-sentence cuts. |
| `SEGMENT_MAX_SECONDS` | 14 | Soft cap for a speaker who never pauses. The cut is placed in a real gap, or deferred up to `SEGMENT_GRACE_SECONDS`. |
| `VAD_MARGIN` | 2.6 | How far above the adaptive noise floor counts as speech. Raise on a noisy line. |

## Tests

```bash
./.venv/bin/python scripts/make_fixtures.py        # build the STT fixture set
./.venv/bin/python scripts/test_stt.py             # endpointing, filtering, decode
./.venv/bin/python scripts/wer.py                  # word error rate on the fixtures
./.venv/bin/python scripts/test_filter.py sonnet   # does it ignore small talk?
./.venv/bin/python scripts/e2e.py                  # full pipeline, real audio
```

`wer.py` is the accuracy gate. It scores the fixtures against their known
references and reports deletions separately, because a mangled word is a
nuisance and a word that never arrives is a question answered wrong.

| Backend | WER | words dropped |
|---|---|---|
| whisper.cpp, 3s timer cuts (before) | 5.3% | 3 |
| faster-whisper, cut at pauses (after) | **0.9%** | **0** |

Measured on 228 reference words of synthetic speech at 0.32x realtime. The
fixtures are `say`-generated and therefore cleaner than a real call — treat the
gap between the two rows as the signal, not the absolute numbers. The alignment
breaks cost ties in favour of a deletion over two substitutions, so the dropped
column errs high; that bias works against the after row, not for it. The three
dropped words were all boundary kills: `rate limit[er]`, `whether the
[reaction] is`, `the rate equation [you] are using`, plus `requests per
second` decoded as `requests per session. Second`.

`test_filter.py` is the one that matters. It feeds realistic noisy transcripts —
weekend chat, screen-share logistics, the candidate's own questions, a follow-up
probe that must beat the original question — and asserts which one gets answered.
