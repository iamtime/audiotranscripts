"""Visual "this and similar ones" for screenshots and auto frames (hide in one click).

Uses the CLIP image embedding from frames_l1 (cached as <image dir>/.emb/<name>.npy; Level 1
writes it for auto frames while recording, the rest is computed on first use). On a slide
talk, different slides were ≤ 0.84 apart and the same talking head at different times
≥ 0.93, so ≥ SIMILAR counts as "the same kind of picture" without catching other slides.
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

SIMILAR = 0.88

_clip = None
_lock = threading.Lock()


def cache_path(image: str | Path) -> Path:
    p = Path(image)
    return p.parent / ".emb" / (p.name + ".npy")


def save_embedding(image: str | Path, emb: np.ndarray):
    c = cache_path(image)
    c.parent.mkdir(exist_ok=True)
    np.save(c, emb.astype(np.float32))


def embedding(image: str | Path) -> np.ndarray | None:
    c = cache_path(image)
    if c.exists():
        try:
            return np.load(c)
        except Exception:
            pass
    global _clip
    with _lock:
        if _clip is None:
            from frames_l1 import ClipFilter
            _clip = ClipFilter(background=False)
    if not _clip.ready.is_set():
        return None
    import cv2
    im = cv2.imread(str(image))
    if im is None:
        return None
    with _lock:
        emb, _ = _clip.embed(im)
    save_embedding(image, emb)
    return emb


def similar_to(target: str, candidates: dict) -> list:
    """Keys of *candidates* ({key: image path}) that look like *target* (an image path)."""
    t = embedding(target)
    if t is None:
        raise RuntimeError("Image similarity needs the CLIP model (frames_l1), which could not be loaded")
    out = []
    for key, path in candidates.items():
        if not Path(path).exists():
            continue
        e = embedding(path)
        if e is not None and float(t @ e) >= SIMILAR:
            out.append(key)
    return out
