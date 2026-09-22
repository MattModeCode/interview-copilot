# Security

## Reporting

Open a [private security advisory](https://github.com/MattModeCode/interview-copilot/security/advisories/new)
rather than a public issue. I'll respond as soon as I see it; this is a
personal project, not a staffed one, so there is no SLA.

## What this program handles

Be aware of what you are running before you file or read a report.

- **Someone else's speech.** The transcript is mostly the *other* party on the
  call. It lives in memory only, is capped at `TRANSCRIPT_MAX_SECONDS`, and is
  wiped after every answer. Nothing is written to disk. The only place it
  leaves the machine is the answering call: to the Anthropic API
  (`BACKEND=api`) or through the `claude` CLI (`BACKEND=cli`).
- **An API key.** `.env` is gitignored and read only by `server/config.py`.
  The key is never logged, never sent to the browser, and never included in a
  WebSocket frame or an error body — API errors are truncated and stripped to
  a type and message.
- **A system-wide hotkey.** `pynput` needs macOS Accessibility permission,
  which is a real privilege. It listens for one chord and does nothing else
  with what it sees. Set `HOTKEY_ENABLED=0` if you'd rather not grant it and
  use the on-screen button.
- **A local server, bound to loopback.** `127.0.0.1` only, no authentication,
  by design: it is a single-user tool on one machine. Do **not** put it on
  `0.0.0.0` or behind a tunnel. There is no auth on the WebSocket, so anything
  that can reach the port can read the transcript and spend your quota.
- **An `ffmpeg` subprocess.** Fixed argument list, no shell, no interpolation
  of anything a caller supplies.

## Out of scope

Reports that amount to "the loopback server has no authentication" or "the
tool can be used covertly" are working as documented; see the disclosure
section of the README for the second one.
