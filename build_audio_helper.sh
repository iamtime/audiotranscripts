#!/bin/bash
# Builds audio_helper (Core Audio process taps → needs the macOS 14.2+ SDK).
# Usage: build_audio_helper.sh [output-path]   (default: ./audio_helper); ARCH=x64 for Intel
set -e
cd "$(dirname "$0")"
OUT="${1:-audio_helper}"
# Xcode 15.0 ships the 14.0 SDK, which lacks the tap API; the Command Line Tools may be newer.
SWIFTC=swiftc; SDKARGS=()
CLT=/Library/Developer/CommandLineTools
if [ -x "$CLT/usr/bin/swiftc" ] && \
   grep -q CATapDescription "$CLT/SDKs/MacOSX.sdk/System/Library/Frameworks/CoreAudio.framework/Headers/AudioHardware.h" 2>/dev/null; then
  SWIFTC="$CLT/usr/bin/swiftc"; SDKARGS=(-sdk "$CLT/SDKs/MacOSX.sdk")
fi
[[ "${ARCH:-arm64}" == x64 ]] && TARGET=x86_64-apple-macos14.2 || TARGET=arm64-apple-macos14.2
"$SWIFTC" -O "${SDKARGS[@]}" -target "$TARGET" -o "$OUT" audio_helper.swift \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker audio_helper.Info.plist
codesign --force --sign - --identifier com.voicerec.audio-helper "$OUT" 2>/dev/null || true
