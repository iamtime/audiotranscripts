#!/bin/bash
# Build "ushi.app" and a DMG for Apple Silicon Macs (macOS 14+):
#   app/dist/ushi-<version>-arm64.dmg
#
#   packaging/build_app.sh                 # everything bundled, incl. all models (~4 GB DMG)
#   MODELS=core packaging/build_app.sh     # without Whisper and Parakeet (downloaded on first use)
#   MODELS=none packaging/build_app.sh     # only Silero VAD; each model is downloaded on first use (~1 GB DMG)
#   REBUILD_PYTHON=1 packaging/build_app.sh
#   ARCH=x64 packaging/build_app.sh        # Intel Macs (PyTorch 2.2.2, see requirements-x64-overrides.txt)
#
# Signing: with a "Developer ID Application" certificate in the keychain the app is
# signed with it (and notarized when APPLE_ID, APPLE_APP_SPECIFIC_PASSWORD and
# APPLE_TEAM_ID are set). Without one it is signed ad hoc: it runs on any Apple
# Silicon Mac, but the first launch needs right-click → Open (see README).
set -euo pipefail
cd "$(dirname "$0")/.."
MODELS="${MODELS:-all}"
export ARCH="${ARCH:-arm64}"
if [[ "$ARCH" == x64 ]]; then
  PYDIR=build/python-x64; RUN=(arch -x86_64); SWIFT_ARCH=x86_64; OUTDIR=mac
else
  PYDIR=build/python; RUN=(); SWIFT_ARCH=arm64; OUTDIR=mac-arm64
fi
PY=(${RUN[@]+"${RUN[@]}"} "$PYDIR/bin/python3")

# 1. Python runtime with all dependencies
if [[ "${REBUILD_PYTHON:-0}" == 1 || ! -x $PYDIR/bin/python3 ]]; then
  packaging/build_python.sh
fi
# The app bundles build/app-python (an APFS clone of the runtime for this ARCH)
rm -rf build/app-python
cp -cR "$PYDIR" build/app-python

# 2. Models
"${PY[@]}" packaging/fetch_models.py --set "$MODELS"
rm -rf build/models
cp -cR "build/models-$MODELS" build/models

# 3. Screenshot helper (macOS 14+)
swiftc -O -target "$SWIFT_ARCH-apple-macos14.0" -o build/screenshot_helper screenshot_helper.swift \
  -framework Cocoa -framework Carbon -framework CoreGraphics
# App audio capture helper (Core Audio process taps; needs the macOS 14.2+ SDK, see the script)
./build_audio_helper.sh build/audio_helper

# 4. App sources
STAGE=build/stage/voice-rec
rm -rf build/stage && mkdir -p "$STAGE"
cp ./*.py "$STAGE"/
cp -R web templates "$STAGE"/
cp build/screenshot_helper build/audio_helper "$STAGE"/
find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +
"${PY[@]}" -m compileall -q "$STAGE" >/dev/null

# 5. Electron (needs Node 18+)
if ! node -e 'process.exit(+process.versions.node.split(".")[0] >= 18 ? 0 : 1)' 2>/dev/null; then
  export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  # shellcheck disable=SC1091
  source "$NVM_DIR/nvm.sh"
  nvm use 22 >/dev/null
fi
[[ -f app/build/icon.icns ]] || "${PY[@]}" packaging/make_icon.py
cd app
npm install --no-audit --no-fund

EXTRA=()
if security find-identity -v -p codesigning 2>/dev/null | grep -q "Developer ID Application"; then
  echo "Signing with Developer ID"
else
  echo "No Developer ID certificate: ad-hoc signing"
  export VOICE_REC_ADHOC=1
  EXTRA+=(-c.mac.identity=null)
fi
SUFFIX=""
[[ "$MODELS" != all ]] && SUFFIX="-$MODELS-models"
rm -rf "dist/$OUTDIR" 2>/dev/null || { sleep 3; rm -rf "dist/$OUTDIR"; }   # Spotlight may still be scanning the last build
npx electron-builder --mac dmg "--$ARCH" -c.mac.target=dmg --publish never "${EXTRA[@]}" \
  "-c.dmg.artifactName=\${productName}-\${version}-\${arch}$SUFFIX.\${ext}"
ls -lh dist/*.dmg
