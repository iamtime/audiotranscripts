#!/usr/bin/env python3
"""audio_helper for Windows: the same command line and stdout protocol as audio_helper.swift.

  audio_helper_win.py --list
      JSON array of capturable sources, in the macOS helper's shape:
      [{"bundle": "system", "name": "System audio (Meet, Slack, Zoom, ...)", "playing": false, "pids": []}]
  audio_helper_win.py --capture <id>
      Raw float32 little-endian, 16 kHz mono on stdout, paced by the wall clock: when
      nothing plays, zeros are written, so the sample count always matches elapsed time.
      Follows the default output device (headphones plugged in, Bluetooth, ...).
      Status lines go to stderr.

Windows captures what the default output device plays (WASAPI loopback): a call in a
browser tab (Google Meet), Slack huddles, Zoom, Teams -- every app at once. Use headphones
with "app + mic", or the microphone hears the other side too. Needs: soundcard, numpy.
"""

import json
import sys
import threading
import time
import warnings
from collections import deque

import numpy as np

RATE = 16000
CHUNK = 1600                  # 100 ms
LATENCY = int(0.3 * RATE)     # how long to wait for late audio before writing silence
MAX_AHEAD = RATE              # more than 1 s ahead of the clock: drop the oldest

SYSTEM = {"bundle": "system", "name": "System audio (Meet, Slack, Zoom, ...)", "playing": False, "pids": []}


def log(msg: str):
    print(f"[audio_helper] {msg}", file=sys.stderr, flush=True)


def list_sources() -> list[dict]:
    src = dict(SYSTEM)
    try:
        import soundcard as sc
        src["name"] = f"System audio: {sc.default_speaker().name}"
    except Exception as e:
        log(f"no output device: {e}")
    return [src]


class Loopback:
    def __init__(self):
        self.fifo: deque[np.ndarray] = deque()
        self.queued = 0
        self.lock = threading.Lock()

    def push(self, mono: np.ndarray):
        with self.lock:
            self.fifo.append(mono)
            self.queued += len(mono)

    def take(self, n: int) -> np.ndarray:
        parts, got = [], 0
        with self.lock:
            while self.fifo and got < n:
                a = self.fifo.popleft()
                if got + len(a) > n:
                    self.fifo.appendleft(a[n - got:])
                    a = a[:n - got]
                parts.append(a)
                got += len(a)
            self.queued -= got
        return np.concatenate(parts) if parts else np.zeros(0, np.float32)

    def drop_to(self, keep: int):
        with self.lock:
            while self.fifo and self.queued - len(self.fifo[0]) >= keep:
                self.queued -= len(self.fifo.popleft())

    def capture(self):
        import soundcard as sc
        warnings.filterwarnings("ignore", message="data discontinuity")
        while True:
            try:
                spk = sc.default_speaker()
                mic = sc.get_microphone(id=str(spk.id), include_loopback=True)
                log(f"capturing: {spk.name}")
                with mic.recorder(samplerate=RATE, blocksize=CHUNK) as rec:
                    checked = time.monotonic()
                    while True:
                        data = rec.record(numframes=CHUNK)
                        if len(data):
                            self.push((data.mean(axis=1) if data.ndim == 2 else data).astype(np.float32))
                        if time.monotonic() - checked > 2:
                            checked = time.monotonic()
                            if sc.default_speaker().id != spk.id:
                                log("output device changed")
                                break
            except Exception as e:
                log(f"capture error: {e}")
                time.sleep(1)

    def run(self):
        threading.Thread(target=self.capture, daemon=True).start()
        out = sys.stdout.buffer
        start, written = time.monotonic(), 0
        while True:
            time.sleep(0.1)
            target = int((time.monotonic() - start) * RATE)
            if self.queued > target - written + MAX_AHEAD:      # device clock ran ahead of ours
                self.drop_to(target - written + MAX_AHEAD // 2)
            block = self.take(target - written)
            deficit = target - written - len(block)
            if deficit > LATENCY:                               # silent or stalled: keep the timeline
                block = np.concatenate([block, np.zeros(deficit - LATENCY, np.float32)])
            if len(block):
                try:
                    out.write(np.clip(block, -1.0, 1.0).astype("<f4").tobytes())
                    out.flush()
                except (BrokenPipeError, OSError):
                    return
                written += len(block)


def main() -> int:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    if args[:1] == ["--list"]:
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(list_sources(), ensure_ascii=False))
        return 0
    if args[:1] == ["--capture"] and len(args) > 1:
        if args[1] != "system":
            log(f"per-app capture is not supported on Windows; capturing system audio instead of {args[1]!r}")
        Loopback().run()
        return 0
    print("Usage: audio_helper_win.py --list | --capture system", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
