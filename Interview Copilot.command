#!/usr/bin/env bash
# Double-click this in Finder. It only exists so macOS will run the real
# launcher in a Terminal window -- .command is the extension Finder honours.
cd "$(dirname "$0")"
exec ./bin/interview-copilot
