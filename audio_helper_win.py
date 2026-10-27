#!/usr/bin/env python3
"""audio_helper for Windows: the same command line and stdout protocol as audio_helper.swift.

  audio_helper_win.py --list
      JSON array of capturable sources, in the macOS helper's shape:
      [{"bundle": "system", "name": "All system audio", "playing": false, "pids": []},
       {"bundle": "chrome.exe", "name": "Google Chrome", "playing": true, "pids": [..]}, ...]
      Apps are listed only where Windows can capture one app (process loopback); otherwise
      the system entry carries a "note" saying why everything is captured.
  audio_helper_win.py --capture <system | app.exe>
      Raw float32 little-endian, 16 kHz mono on stdout, paced by the wall clock: when
      nothing plays, zeros are written, so the sample count always matches elapsed time.
      Status lines go to stderr.

Sources:
- system: what the default output device plays (WASAPI loopback; follows device changes).
- an app (e.g. chrome.exe for Google Meet in the browser, slack.exe for huddles): WASAPI
  process loopback of the app's process tree. Windows supports it from Windows 10 2004
  on most builds, officially from build 20348 / Windows 11; this helper tries it and
  falls back to the system audio (with a warning) where it fails.
Use headphones with "app + mic", or the microphone hears the other side too.
Needs: soundcard, numpy; for apps also comtypes, pycaw, psutil.
"""

import json
import os
import sys
import threading
import time
import warnings
from collections import deque

sys.coinit_flags = 0          # COM in the multithreaded apartment (before comtypes is imported)

import numpy as np

RATE = 16000
CHUNK = 1600                  # 100 ms
LATENCY = int(0.3 * RATE)     # how long to wait for late audio before writing silence
MAX_AHEAD = RATE              # more than 1 s ahead of the clock: drop the oldest
SYSTEM = "system"
NO_APPS = ("This Windows can't record a single app (it needs Windows 10 2004+ with process loopback, "
           "or Windows 11): all system audio is recorded - mute other sounds")
# Parents that start apps rather than being the app the user knows.
LAUNCHERS = {"explorer.exe", "svchost.exe", "services.exe", "wininit.exe", "winlogon.exe", "sihost.exe",
             "runtimebroker.exe", "applicationframehost.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
             "windowsterminal.exe", "openconsole.exe", "conhost.exe", "bash.exe", "mintty.exe", "code.exe",
             "taskmgr.exe", "userinit.exe", "smss.exe", "csrss.exe", "system"}


def log(msg: str):
    print(f"[audio_helper] {msg}", file=sys.stderr, flush=True)


# ── Process loopback (one app's audio) ───────────────────────────────────────

_api = None


def _loopback_api():
    """COM declarations for ActivateAudioInterfaceAsync + IAudioClient, built on first use."""
    global _api
    if _api is not None:
        return _api
    import ctypes
    from ctypes import POINTER, Structure, byref, c_float, c_int, c_long, c_longlong, c_ubyte, c_uint32, \
        c_uint64, c_ulong, c_ushort, c_void_p, c_wchar_p
    from ctypes.wintypes import DWORD, HANDLE, WORD
    import comtypes
    from comtypes import COMMETHOD, GUID, HRESULT, COMObject, IUnknown

    class WAVEFORMATEX(Structure):
        _fields_ = [("wFormatTag", WORD), ("nChannels", WORD), ("nSamplesPerSec", DWORD),
                    ("nAvgBytesPerSec", DWORD), ("nBlockAlign", WORD), ("wBitsPerSample", WORD), ("cbSize", WORD)]

    class IAudioClient(IUnknown):
        _iid_ = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
        _methods_ = [
            COMMETHOD([], HRESULT, "Initialize", (["in"], c_int, "mode"), (["in"], DWORD, "flags"),
                      (["in"], c_longlong, "duration"), (["in"], c_longlong, "period"),
                      (["in"], POINTER(WAVEFORMATEX), "fmt"), (["in"], POINTER(GUID), "session")),
            COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(c_uint32), "n")),
            COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(c_longlong), "t")),
            COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(c_uint32), "n")),
            COMMETHOD([], HRESULT, "IsFormatSupported", (["in"], c_int, "mode"), (["in"], POINTER(WAVEFORMATEX), "f"),
                      (["out"], POINTER(POINTER(WAVEFORMATEX)), "closest")),
            COMMETHOD([], HRESULT, "GetMixFormat", (["out"], POINTER(POINTER(WAVEFORMATEX)), "f")),
            COMMETHOD([], HRESULT, "GetDevicePeriod", (["out"], POINTER(c_longlong), "a"),
                      (["out"], POINTER(c_longlong), "b")),
            COMMETHOD([], HRESULT, "Start"),
            COMMETHOD([], HRESULT, "Stop"),
            COMMETHOD([], HRESULT, "Reset"),
            COMMETHOD([], HRESULT, "SetEventHandle", (["in"], HANDLE, "h")),
            COMMETHOD([], HRESULT, "GetService", (["in"], POINTER(GUID), "riid"), (["out"], POINTER(c_void_p), "ppv")),
        ]

    class IAudioCaptureClient(IUnknown):
        _iid_ = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetBuffer", (["out"], POINTER(POINTER(c_ubyte)), "data"),
                      (["out"], POINTER(c_uint32), "frames"), (["out"], POINTER(DWORD), "flags"),
                      (["out"], POINTER(c_uint64), "pos"), (["out"], POINTER(c_uint64), "qpc")),
            COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "frames")),
            COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "frames")),
        ]

    class IActivateAudioInterfaceAsyncOperation(IUnknown):
        _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
        _methods_ = [COMMETHOD([], HRESULT, "GetActivateResult", (["out"], POINTER(c_long), "hr"),
                               (["out"], POINTER(POINTER(IUnknown)), "unk"))]

    class IActivateAudioInterfaceCompletionHandler(IUnknown):
        _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
        _methods_ = [COMMETHOD([], HRESULT, "ActivateCompleted",
                               (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "op"))]

    class IAgileObject(IUnknown):   # the completion arrives on another thread
        _iid_ = GUID("{94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90}")
        _methods_ = []

    class Done(COMObject):
        _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

        def __init__(self):
            super().__init__()
            self.event = threading.Event()

        def ActivateCompleted(self, this, op):
            self.event.set()
            return 0

    class LOOPBACK_PARAMS(Structure):
        _fields_ = [("ActivationType", c_int), ("TargetProcessId", DWORD), ("ProcessLoopbackMode", c_int)]

    class BLOB(Structure):
        _fields_ = [("cbSize", c_ulong), ("pBlobData", c_void_p)]

    class PROPVARIANT(Structure):
        _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort), ("blob", BLOB)]

    activate = ctypes.WinDLL("Mmdevapi").ActivateAudioInterfaceAsync
    activate.argtypes = [c_wchar_p, POINTER(GUID), POINTER(PROPVARIANT),
                         POINTER(IActivateAudioInterfaceCompletionHandler),
                         POINTER(POINTER(IActivateAudioInterfaceAsyncOperation))]
    activate.restype = HRESULT

    def open_client(pid: int):
        """An IAudioClient capturing pid and its child processes, started, 16 kHz mono float."""
        params = LOOPBACK_PARAMS(1, pid, 0)   # PROCESS_LOOPBACK, INCLUDE_TARGET_PROCESS_TREE
        pv = PROPVARIANT(vt=65, blob=BLOB(ctypes.sizeof(params), ctypes.cast(byref(params), c_void_p)))  # VT_BLOB
        done = Done()
        op = POINTER(IActivateAudioInterfaceAsyncOperation)()
        activate("VAD\\Process_Loopback", byref(IAudioClient._iid_), byref(pv),
                 done.QueryInterface(IActivateAudioInterfaceCompletionHandler), byref(op))
        if not done.event.wait(5):
            raise RuntimeError("process loopback activation timed out")
        hr, unk = op.GetActivateResult()
        if hr < 0:
            raise OSError(hr & 0xFFFFFFFF, f"process loopback activation failed (0x{hr & 0xFFFFFFFF:08X})")
        client = unk.QueryInterface(IAudioClient)
        fmt = WAVEFORMATEX(3, 1, RATE, RATE * 4, 4, 32, 0)    # IEEE float, mono, 16 kHz
        # LOOPBACK | AUTOCONVERTPCM | SRC_DEFAULT_QUALITY: Windows resamples and downmixes for us
        client.Initialize(0, 0x00020000 | 0x80000000 | 0x08000000, 2_000_000, 0, byref(fmt), None)
        cap = ctypes.cast(c_void_p(client.GetService(byref(IAudioCaptureClient._iid_))),
                          POINTER(IAudioCaptureClient))
        client.Start()
        return client, cap

    def read(cap) -> list[np.ndarray]:
        out = []
        n = cap.GetNextPacketSize()
        while n:
            data, frames, flags, _, _ = cap.GetBuffer()
            if flags & 2 or not frames:          # AUDCLNT_BUFFERFLAGS_SILENT
                out.append(np.zeros(frames, np.float32))
            else:
                out.append(np.ctypeslib.as_array(ctypes.cast(data, POINTER(c_float)), shape=(frames,)).copy())
            cap.ReleaseBuffer(frames)
            n = cap.GetNextPacketSize()
        return out

    _api = {"open": open_client, "read": read, "com_init": lambda: comtypes.CoInitializeEx(0)}
    return _api


def apps_supported() -> tuple[bool, str]:
    """Can this Windows capture one app? Try it on ourselves (a no-op capture)."""
    try:
        api = _loopback_api()
        client, _cap = api["open"](os.getpid())
        client.Stop()
        return True, ""
    except Exception as e:
        return False, str(e)


# ── Apps playing audio ───────────────────────────────────────────────────────

def _app_root(proc, psutil):
    """The process the user knows: climb from a helper (Chrome's audio service, Teams'
    WebView2) to the topmost ancestor below a launcher (Explorer, a shell)."""
    root = proc
    try:
        parent = proc.parent()
        while parent is not None and parent.name().lower() not in LAUNCHERS:
            root, parent = parent, parent.parent()
    except psutil.Error:
        pass
    return root


def list_apps() -> list[dict]:
    import psutil
    from pycaw.pycaw import AudioUtilities
    import native_helpers
    mine = {os.getpid()}
    try:
        mine |= {p.pid for p in psutil.Process().parents()}
    except psutil.Error:
        pass
    apps: dict[str, dict] = {}
    for s in AudioUtilities.GetAllSessions():
        proc = s.Process
        if proc is None:
            continue
        try:
            root = _app_root(proc, psutil)
            if root.pid in mine:
                continue
            exe = root.name().lower()
            path = root.exe()
        except psutil.Error:
            continue
        a = apps.setdefault(exe, {"bundle": exe, "name": native_helpers.app_display_name(path) or exe,
                                  "playing": False, "pids": []})
        a["playing"] = a["playing"] or s.State == 1          # AudioSessionStateActive
        a["pids"].append(proc.pid)
    return sorted(apps.values(), key=lambda a: (not a["playing"], a["name"].lower()))


def resolve_root(exe: str) -> int | None:
    """The app's main process: the one of its name with the biggest process tree."""
    import psutil
    best, best_size = None, -1
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if (p.info["name"] or "").lower() != exe:
                continue
            parent = p.parent()
            if parent is not None and parent.name().lower() == exe:
                continue                                   # a child of the same app
            size = len(p.children(recursive=True))
            if size > best_size:
                best, best_size = p.pid, size
        except psutil.Error:
            continue
    return best


def list_sources() -> list[dict]:
    system = {"bundle": SYSTEM, "name": "All system audio", "playing": False, "pids": []}
    try:
        import soundcard as sc
        system["name"] = f"All system audio ({sc.default_speaker().name})"
    except Exception as e:
        log(f"no output device: {e}")
    ok, why = apps_supported()
    if not ok:
        log(f"per-app capture unavailable: {why}")
        system["note"] = NO_APPS
        return [system]
    try:
        apps = list_apps()
    except Exception as e:
        log(f"could not list apps: {e}")
        apps = []
    return [system, *apps]


# ── Capture ──────────────────────────────────────────────────────────────────

class Fifo:
    def __init__(self):
        self.parts: deque[np.ndarray] = deque()
        self.queued = 0
        self.lock = threading.Lock()

    def push(self, mono: np.ndarray):
        with self.lock:
            self.parts.append(mono)
            self.queued += len(mono)

    def take(self, n: int) -> np.ndarray:
        out, got = [], 0
        with self.lock:
            while self.parts and got < n:
                a = self.parts.popleft()
                if got + len(a) > n:
                    self.parts.appendleft(a[n - got:])
                    a = a[:n - got]
                out.append(a)
                got += len(a)
            self.queued -= got
        return np.concatenate(out) if out else np.zeros(0, np.float32)

    def drop_to(self, keep: int):
        with self.lock:
            while self.parts and self.queued - len(self.parts[0]) >= keep:
                self.queued -= len(self.parts.popleft())


def capture_system(fifo: Fifo):
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
                        fifo.push((data.mean(axis=1) if data.ndim == 2 else data).astype(np.float32))
                    if time.monotonic() - checked > 2:
                        checked = time.monotonic()
                        if sc.default_speaker().id != spk.id:
                            log("output device changed")
                            break
        except Exception as e:
            log(f"capture error: {e}")
            time.sleep(1)


def capture_app(fifo: Fifo, exe: str):
    """Follow the app: wait while it isn't running, re-attach when it restarts."""
    import psutil
    api = _loopback_api()
    api["com_init"]()
    waiting_logged = False
    while True:
        pid = resolve_root(exe)
        if pid is None:
            if not waiting_logged:
                log(f"{exe} is not running; waiting for it")
                waiting_logged = True
            time.sleep(2)
            continue
        waiting_logged = False
        try:
            client, cap = api["open"](pid)
            log(f"capturing: {exe} (pid {pid} and its child processes)")
            while psutil.pid_exists(pid):
                for block in api["read"](cap):
                    fifo.push(block)
                time.sleep(0.01)
            client.Stop()
            log(f"{exe} exited")
        except Exception as e:
            log(f"capture error: {e}")
            time.sleep(2)


def pace(fifo: Fifo):
    """Write the fifo to stdout at the wall clock's pace, zeros where nothing came."""
    out = sys.stdout.buffer
    start, written = time.monotonic(), 0
    while True:
        time.sleep(0.1)
        target = int((time.monotonic() - start) * RATE)
        if fifo.queued > target - written + MAX_AHEAD:     # device clock ran ahead of ours
            fifo.drop_to(target - written + MAX_AHEAD // 2)
        block = fifo.take(target - written)
        deficit = target - written - len(block)
        if deficit > LATENCY:                              # silent or stalled: keep the timeline
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
        source = args[1].lower()
        fifo = Fifo()
        target = capture_system
        if source != SYSTEM:
            ok, why = apps_supported()
            if ok:
                target = lambda f: capture_app(f, source)
            else:
                log(f"warning: {NO_APPS} ({why})")
        threading.Thread(target=target, args=(fifo,), daemon=True).start()
        pace(fifo)
        return 0
    print("Usage: audio_helper_win.py --list | --capture <system | app.exe>", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
