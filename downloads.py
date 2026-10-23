"""Visible progress for models that are downloaded on first use.

The libraries (faster-whisper, onnx-asr, transformers, SpeechBrain, EasyOCR)
download their models themselves; this module only watches the cache folder
grow while a model loads and reports it, so a first start without bundled
models shows "Downloading Whisper large-v3-turbo: 340 of ~1620 MB" instead of
a silent wait. (Growth is visible with plain HTTP downloads; the macOS app sets
HF_HUB_DISABLE_XET=1, as Xet writes each file only when it is complete.)

    with downloads.watch_hf("mobiuslabsgmbh/faster-whisper-large-v3-turbo", "Whisper large-v3-turbo"):
        model = WhisperModel(...)

Progress goes to the reporter set with set_reporter(fn(event: dict)); by default
a line on stderr every 10 %. Events: {"label", "done_mb", "total_mb", "finished",
"error"}.
"""

import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

# Approximate download sizes (MB) of what each library fetches, for the progress bar.
SIZES_MB = {
    "mobiuslabsgmbh/faster-whisper-large-v3-turbo": 1620,
    "istupakov/gigaam-v3-onnx": 226,
    "istupakov/parakeet-tdt-0.6b-v3-onnx": 670,
    "openai/clip-vit-base-patch32": 610,
    "speechbrain/spkrec-ecapa-voxceleb": 88,
    "easyocr": 98,
}
ENGINE_REPOS = {   # main.py --engine -> model repo (default model of each engine)
    "whisper": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "gigaam": "istupakov/gigaam-v3-onnx",
    "parakeet": "istupakov/parakeet-tdt-0.6b-v3-onnx",
}
POLL_SEC = 0.7
MIN_MB = 1.0    # growth below this is cache bookkeeping, not a download


def _stderr_reporter():
    last = {}

    def report(ev):
        label = ev["label"]
        if ev.get("error") or ev.get("finished"):
            if label in last:
                print(f"[download] {label}: {'failed: ' + ev['error'] if ev.get('error') else 'done'}",
                      file=sys.stderr)
            last.pop(label, None)
            return
        step = int(100 * ev["done_mb"] / ev["total_mb"]) // 10 if ev.get("total_mb") else int(ev["done_mb"]) // 100
        if last.get(label) != step:
            last[label] = step
            total = f" of ~{ev['total_mb']:.0f}" if ev.get("total_mb") else ""
            print(f"[download] {label}: {ev['done_mb']:.0f}{total} MB", file=sys.stderr)
    return report


_reporter = _stderr_reporter()


def set_reporter(fn):
    global _reporter
    _reporter = fn


def hf_hub_dir() -> Path:
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"]).expanduser()
    home = os.environ.get("HF_HOME") or Path(os.environ.get("XDG_CACHE_HOME") or "~/.cache") / "huggingface"
    return Path(home).expanduser() / "hub"


def hf_repo_dir(repo: str) -> Path:
    return hf_hub_dir() / ("models--" + repo.replace("/", "--"))


def hf_cached(repo: str) -> bool:
    snaps = hf_repo_dir(repo) / "snapshots"
    return snaps.is_dir() and any(f.is_file() for f in snaps.rglob("*"))


def engine_models() -> dict:
    """{engine: {"cached": bool, "mb": approximate download size}} for the UI."""
    return {e: {"cached": hf_cached(r), "mb": SIZES_MB[r]} for e, r in ENGINE_REPOS.items()}


def easyocr_dir() -> Path:
    base = os.environ.get("EASYOCR_MODULE_PATH") or os.environ.get("MODULE_PATH") or "~/.EasyOCR"
    return Path(base).expanduser() / "model"


def _size(path: Path) -> int:
    total = 0
    if path.exists():
        for f in path.rglob("*"):
            try:
                if f.is_file() and not f.is_symlink():
                    total += f.stat().st_size
            except OSError:
                pass
    return total


@contextmanager
def watch(path: Path, label: str, total_mb: float | None = None):
    """Report the growth of *path* while the body runs (it is downloading into it)."""
    start = _size(path)
    stop = threading.Event()
    seen = {"any": False, "last": None}

    def poll():
        while not stop.wait(POLL_SEC):
            done = (_size(path) - start) / 1e6
            if done >= MIN_MB and done != seen["last"]:
                seen["last"] = done
                seen["any"] = True
                _safe_report({"label": label, "done_mb": round(done, 1),
                              "total_mb": max(total_mb or 0, done) or None, "finished": False})

    t = threading.Thread(target=poll, daemon=True, name=f"download-watch:{label}")
    t.start()
    try:
        yield
    except BaseException as e:
        stop.set()
        t.join()
        if seen["any"] or not _size(path):
            _safe_report({"label": label, "finished": True, "error": _explain(e)})
        raise
    stop.set()
    t.join()
    if seen["any"]:
        _safe_report({"label": label, "finished": True})


def watch_hf(repo: str, label: str):
    return watch(hf_repo_dir(repo), label, SIZES_MB.get(repo))


def watch_easyocr():
    return watch(easyocr_dir(), "EasyOCR (screenshot text)", SIZES_MB["easyocr"])


def _explain(e: BaseException) -> str:
    text = f"{type(e).__name__}: {e}"
    offline = ("ConnectionError", "LocalEntryNotFoundError", "URLError", "MaxRetryError",
               "NameResolutionError", "Temporary failure", "nodename nor servname", "OfflineModeIsEnabled")
    if any(k in text for k in offline):
        return "no internet connection (the model is downloaded once, on first use)"
    return text[:300]


def _safe_report(ev):
    try:
        _reporter(ev)
    except Exception:
        pass
