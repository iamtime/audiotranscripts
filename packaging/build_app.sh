#!/bin/bash
# Build "Voice Rec.app" and a DMG for Apple Silicon Macs (macOS 14+):
#   app/dist/Voice Rec-<version>-arm64.dmg
#
#   packaging/build_app.sh                 # everything bundled, incl. all models (~4 GB DMG)
#   MODELS=core packaging/build_app.sh     # without Whisper and Parakeet (downloaded on first use)
#   MODELS=none packaging/build_app.sh     # only Silero VAD; each model is downloaded on first use (~1 GB DMG)
#   REBUILD_PYTHON=1 packaging/build_app.sh
#
# Signing: with a "Developer ID Application" certificate in the keychain the app is
# signed with it (and notarized when APPLE_ID, APPLE_APP_SPECIFIC_PASSWORD and
# APPLE_TEAM_ID are set). Without one it is signed ad hoc: it runs on any Apple
# Silicon Mac, but the first launch needs right-click → Open (see README).
set -euo pipefail
cd "$(dirname "$0")/.."
MODELS="${MODELS:-all}"

# 1. Python runtime with all dependencies
if [[ "${REBUILD_PYTHON:-0}" == 1 || ! -x build/python/bin/python3 ]]; then
  packaging/build_python.sh
fi

# 2. Models
build/python/bin/python3 packaging/fetch_models.py --set "$MODELS"
rm -rf build/models
cp -cR "build/models-$MODELS" build/models

# 3. Screenshot helper (arm64, macOS 14+)
swiftc -O -target arm64-apple-macos14.0 -o build/screenshot_helper screenshot_helper.swift \
  -framework Cocoa -framework Carbon -framework CoreGraphics

# 4. App sources
STAGE=build/stage/voice-rec
rm -rf build/stage && mkdir -p "$STAGE"
cp ./*.py "$STAGE"/
cp -R web templates "$STAGE"/
cp build/screenshot_helper "$STAGE"/
find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +
build/python/bin/python3 -m compileall -q "$STAGE" >/dev/null

# 5. Electron (needs Node 18+)
if ! node -e 'process.exit(+process.versions.node.split(".")[0] >= 18 ? 0 : 1)' 2>/dev/null; then
  export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  # shellcheck disable=SC1091
  source "$NVM_DIR/nvm.sh"
  nvm use 22 >/dev/null
fi
[[ -f app/build/icon.icns ]] || build/python/bin/python3 packaging/make_icon.py
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
rm -rf dist/mac-arm64
npx electron-builder --mac dmg --arm64 --publish never "${EXTRA[@]}" \
  "-c.dmg.artifactName=\${productName}-\${version}-\${arch}$SUFFIX.\${ext}"
ls -lh dist/*.dmg
