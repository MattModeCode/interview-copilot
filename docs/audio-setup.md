# Audio setup

The goal: the interviewer's voice must reach **both** your ears and BlackHole.

## Build the Multi-Output Device (once)

1. Open **Audio MIDI Setup** (`/Applications/Utilities`).
2. Bottom-left **+** → **Create Multi-Output Device**.
3. Tick **BlackHole 2ch** and your real output (MacBook Pro Speakers, or your
   headphones — see the headphone caveat below).
4. Put your **real output first** in the list. The first device is the clock
   master; making BlackHole the master causes drift and crackling.
5. Tick **Drift Correction** on BlackHole, not on the master.

## Before every call

```bash
SwitchAudioSource -t output -s "Multi-Output Device"
```

And inside Teams: **Settings → Devices → Speaker → Multi-Output Device**. Teams
keeps its own device selection separate from the system one, and it silently
reverts after some updates. Check it every time — `preflight.sh` cannot see
Teams' internal setting.

## Gotchas

**You hear nothing.** Output is set to raw *BlackHole 2ch* instead of the
Multi-Output Device. BlackHole is a loopback with no speakers attached.

**Volume slider does nothing.** Expected — a Multi-Output Device has no master
volume. Set the level on the underlying device first, or use the Teams slider.

**Bluetooth headphones drop to mono / sound terrible.** Multi-Output forces the
A2DP profile off on some devices. Use wired headphones for the call, or accept
speaker audio. Wired is also better because speaker audio can echo back into
your mic and land in the transcript twice.

**Transcript is empty but the meeting has sound.** Almost always Teams' own
speaker setting. Second most likely: `AUDIO_DEVICE` in `.env` doesn't match the
current ffmpeg index — indices shift when you plug in a USB device. Re-check:

```bash
ffmpeg -f avfoundation -list_devices true -i ""
```

**The hotkey does nothing.** macOS Accessibility permission. System Settings →
Privacy & Security → Accessibility → add the terminal you launched from. The
on-screen button always works regardless.

**Restore afterwards.**

```bash
SwitchAudioSource -t output -s "MacBook Pro Speakers"
```
