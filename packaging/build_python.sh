#!/bin/bash
# Build the self-contained Python runtime for the macOS app: build/python
# (a relocatable python-build-standalone CPython with all recorder/server deps).
set -euo pipefail
cd "$(dirname "$0")/.."

PY_VERSION="${PY_VERSION:-3.12.11}"
OUT=build/python

uv python install "$PY_VERSION"
SRC="$(uv python find --managed-python "$PY_VERSION")"
SRC="$(cd "$(dirname "$SRC")/.." && pwd -P)"

rm -rf "$OUT"
mkdir -p build
cp -cR "$SRC" "$OUT" 2>/dev/null || cp -R "$SRC" "$OUT"
rm -f "$OUT"/lib/python3.*/EXTERNALLY-MANAGED

uv pip install --python "$OUT/bin/python3" --link-mode copy -r packaging/requirements-app.txt

# Drop what the app never uses (headers, tests, Tk, docs) to save ~0.5 GB.
SP="$("$OUT/bin/python3" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
rm -rf "$OUT/include" "$OUT/share" \
       "$OUT"/lib/python3.*/{test,idlelib,tkinter,turtledemo,ensurepip} \
       "$OUT"/lib/libtcl* "$OUT"/lib/libtk* "$OUT"/lib/tcl* "$OUT"/lib/tk* \
       "$SP"/torch/include "$SP"/torch/share/cmake

# Static ffmpeg from imageio-ffmpeg, under a name main.py finds on PATH.
FF="$(ls "$SP"/imageio_ffmpeg/binaries/ffmpeg-* | head -1)"
mkdir -p "$OUT/extra-bin"
cp "$FF" "$OUT/extra-bin/ffmpeg"
chmod +x "$OUT/extra-bin/ffmpeg"

"$OUT/bin/python3" -m compileall -q -j 0 "$SP" >/dev/null || true
"$OUT/bin/python3" -c "import faster_whisper, onnx_asr, sounddevice, easyocr, torch, speechbrain, transformers, fastapi, uvicorn, cv2; print('python ok:', torch.__version__)"
du -sh "$OUT"
