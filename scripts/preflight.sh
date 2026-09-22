#!/usr/bin/env bash
# Run this 10 minutes before the interview, not 1 minute before.
cd "$(dirname "$0")/.."
PASS=0; FAIL=0; WARN=0
ok(){ printf "  \033[32mok\033[0m    %s\n" "$1"; PASS=$((PASS+1)); }
no(){ printf "  \033[31mFAIL\033[0m  %s\n" "$1"; FAIL=$((FAIL+1)); }
wa(){ printf "  \033[33mwarn\033[0m  %s\n" "$1"; WARN=$((WARN+1)); }

set -a; [ -f .env ] && . ./.env; set +a

echo "Interview Copilot preflight"
echo
echo "Audio"
if system_profiler SPAudioDataType 2>/dev/null | grep -q "BlackHole 2ch"; then
  ok "BlackHole 2ch is installed"
else
  no "BlackHole 2ch not found -- brew install blackhole-2ch"
fi

OUT=$(SwitchAudioSource -c -t output 2>/dev/null || echo "?")
if [ "$OUT" = "Multi-Output Device" ]; then
  ok "system output is Multi-Output Device (you hear it AND we capture it)"
elif [ "$OUT" = "BlackHole 2ch" ]; then
  wa "output is raw BlackHole -- capture works but YOU WILL HEAR NOTHING. Use Multi-Output Device."
else
  no "system output is '$OUT'. Switch to Multi-Output Device or nothing is captured."
  echo "        SwitchAudioSource -t output -s 'Multi-Output Device'"
fi

IDX=$(ffmpeg -f avfoundation -list_devices true -i "" 2>&1 | grep "BlackHole 2ch" | sed -n 's/.*\[\([0-9]*\)\] BlackHole.*/\1/p' | head -1)
if [ -n "$IDX" ]; then
  if [ "$IDX" = "${AUDIO_DEVICE:-1}" ]; then ok "BlackHole is device index $IDX, matches AUDIO_DEVICE"
  else no "BlackHole is index $IDX but AUDIO_DEVICE=${AUDIO_DEVICE:-1}. Fix .env."; fi
else
  no "ffmpeg cannot see BlackHole"
fi

echo
echo "Transcription"
[ -f "${WHISPER_MODEL:-models/ggml-large-v3-turbo.bin}" ] \
  && ok "whisper model present" || no "whisper model missing: ${WHISPER_MODEL}"
command -v whisper-server >/dev/null && ok "whisper-server on PATH" || no "whisper-server missing -- brew install whisper-cpp"
command -v ffmpeg >/dev/null && ok "ffmpeg on PATH" || no "ffmpeg missing -- brew install ffmpeg"

echo
echo "Models"
CB="${CLAUDE_BIN:-claude}"
if [ -x "$CB" ]; then
  ok "claude CLI found at $CB"
  # This is the check that matters: a live turn proves auth AND quota. A
  # subscription session limit only shows up when you actually ask something.
  RESP=$(echo "Reply with exactly: READY" | timeout 60 "$CB" -p --model sonnet \
      --strict-mcp-config --mcp-config '{"mcpServers":{}}' --setting-sources '' \
      --system-prompt "Reply with exactly one word." --restricted \
      --no-session-persistence 2>&1 | tr '\n' ' ')
  case "$(echo "$RESP" | tr 'A-Z' 'a-z')" in
    *"session limit"*|*"usage limit"*|*"quota"*|*"rate limit"*)
      no "SUBSCRIPTION LIMIT ALREADY REACHED -- $(echo "$RESP" | head -c 90)" ;;
    *ready*)
      ok "claude subscription is live and has quota" ;;
    *)
      no "claude CLI did not answer: $(echo "$RESP" | head -c 90)" ;;
  esac
else
  no "claude CLI not found at $CB -- set CLAUDE_BIN in .env"
fi
echo "        model: ${CLAUDE_MODEL:-sonnet}"

echo
echo "Live check"
if [ "$FAIL" -eq 0 ]; then
  echo "  speaking a test phrase through the output device..."
  rm -f /tmp/pf_*.wav
  ( timeout 7 ffmpeg -hide_banner -loglevel error -f avfoundation -i ":${AUDIO_DEVICE:-1}" \
      -ac 1 -ar 16000 -f segment -segment_time 3 /tmp/pf_%02d.wav >/dev/null 2>&1 & ) 
  sleep 1
  say -v Samantha "Testing one two three, can you hear this"
  sleep 4
  BIG=$(ls -S /tmp/pf_*.wav 2>/dev/null | head -1)
  if [ -n "$BIG" ]; then
    SZ=$(stat -f%z "$BIG")
    LOUD=$(ffmpeg -hide_banner -i "$BIG" -af volumedetect -f null - 2>&1 | grep max_volume | awk '{print $5}')
    if [ -n "$LOUD" ] && [ "${LOUD%.*}" -gt -60 ] 2>/dev/null; then
      ok "captured audio, peak ${LOUD}dB -- signal is reaching BlackHole"
    else
      no "captured only silence (peak ${LOUD:-?}dB). Audio is NOT routed into BlackHole."
    fi
  else
    no "no audio captured at all"
  fi
  rm -f /tmp/pf_*.wav
else
  wa "skipped -- fix the failures above first"
fi

echo
printf "%d ok, %d warn, %d FAIL\n" "$PASS" "$WARN" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
