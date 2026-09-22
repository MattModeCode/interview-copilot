# Contributing

Small project, few rules.

## Before you start

It's macOS-only and it will stay that way unless someone brings a working
loopback-capture path for another platform. The capture layer is
`avfoundation` + [BlackHole](https://github.com/ExistentialAudio/BlackHole);
everything above `server/capture.py` is portable, nothing below it is.

Please open an issue before a large change. An interview tool has a narrow
job and the answer to most feature requests is no — the value here is that
there is one screen, one keypress, and nothing to think about mid-call.

## Setup

```bash
brew install blackhole-2ch ffmpeg switchaudio-osx
./bin/interview-copilot          # creates .venv, installs, launches
```

## Running the checks

```bash
./scripts/preflight.sh      # audio + backend, live. Costs one model turn.
./scripts/test_filter.py    # does the prompt find the right question? 5 turns.
./scripts/test_stt.py       # transcription accuracy. Needs fixtures.
./scripts/wer.py            # word error rate against a reference.
./scripts/e2e.py            # whole loop, audio in to answer out.
```

`test_filter.py` and `preflight.sh` make **real model calls** and cost real
money (`BACKEND=api`) or real subscription quota (`BACKEND=cli`). CI does not
run them for that reason; it lints and compiles only. Run them locally before
opening a PR that touches the prompt, the backends, or the capture path, and
say in the PR which ones you ran.

`test_stt.py`, `wer.py` and `e2e.py` need audio fixtures, which are not in the
repo (`fixtures/` is gitignored — recorded speech isn't mine to publish).
Generate your own:

```bash
./scripts/make_fixtures.py   # synthesizes them with macOS `say`
```

## What not to commit

`.env`, `domain.md`, `fixtures/`, anything under `.venv/`, and any editor or
agent config. All of it is already in `.gitignore`. `domain.md` in particular
describes a real interview — the checked-in `domain.md.example` is a template
and the only version that belongs here.

## Style

Match what's there. The code carries load-bearing comments explaining *why* a
thing is the way it is — several of them record a design that was tried and
failed. Keep that habit; delete a comment only when you've disproven it.
