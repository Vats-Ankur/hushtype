#!/usr/bin/env bash
# One-time setup for HushType (formerly LocalFlow). Downloads & builds whisper.cpp, fetches a model,
# and creates a Python venv with pynput. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"
MODEL="${1:-base.en}"   # ./setup.sh small.en  for the more accurate model

echo "==> whisper.cpp source"
if [ ! -d whisper.cpp ]; then
  git clone --depth 1 https://github.com/ggml-org/whisper.cpp.git
fi

echo "==> building whisper.cpp (a few minutes on a Pi 5)"
cmake -S whisper.cpp -B whisper.cpp/build -DCMAKE_BUILD_TYPE=Release -DWHISPER_SDL2=OFF >/dev/null
cmake --build whisper.cpp/build -j"$(nproc)" --config Release --target whisper-server whisper-cli

echo "==> model: $MODEL"
[ -f "whisper.cpp/models/ggml-$MODEL.bin" ] || sh whisper.cpp/models/download-ggml-model.sh "$MODEL"

echo "==> python venv"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pynput

chmod +x hushtype.py
echo
echo "Done. Start it with:  ./run.sh"
