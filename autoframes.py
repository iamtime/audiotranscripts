#!/usr/bin/env python3
"""Automatic screen frames: keep only the moments worth keeping from a ~1 fps capture stream.

Cascade, cheap → expensive:
  Level 0 (here)    pixels. A frame becomes a candidate only once the screen has stood still
                    for STABLE_SEC after a change (transitions, scrolling in progress, typing
                    and talking heads never qualify). Regions that move all the time (a webcam
                    picture-in-picture) are masked out. A candidate opens a new *scene* when it
                    differs enough from the current one; otherwise it extends it.
  Level 1 (planned) image embeddings: semantic dedup + zero-shot class (face / slide / code…).
                    Plugs in as a `FrameFilter` run synchronously on candidates.
  Level 2 (planned) OCR + VLM caption. Plugs in as an `Enricher` run on a background queue;
                    it fills the scene's ocr/cls/caption/title fields.

A scene is a frame plus the interval it was on screen. Scenes live in
projects/<project>/frames/<sid>/scenes.json next to their JPEGs; they are not written into
the transcript (the transcript is timestamped, so they are matched to speech by time).
Picking the few that belong in the text is select_frames.py.

Offline (for tuning thresholds on a recorded talk):
  python autoframes.py talk.mp4 --out /tmp/frames [--truth projects/x/screenshots]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import shutil
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np


# ── Parameters ───────────────────────────────────────────────────────────────

@dataclass
class Params:
    width: int = 256            # frames are compared at this width (grayscale)
    pix_thresh: int = 18        # 0..255 difference for a pixel to count as changed
    still_frac: float = 0.004   # < this share of (unmasked) pixels moved → frame is "still"
    stable_sec: float = 1.5     # still this long after a change → candidate
    scene_frac: float = 0.03    # candidate's changed area vs the scene above this → new scene
    drift_frac: float = 0.005   # …above this (but less than scene_frac) → keep as the scene's "last" state
    sparse_rel: float = 0.5     # …or more than this share of the slide's content changed (sparse
    sparse_min: float = 0.004   #    slides: a new title on a dark slide is < 3% of the frame), if ≥ sparse_min
    motion_window_sec: float = 10.0   # motion history used to find constantly moving regions
    motion_hot: float = 0.3     # pixel moved in ≥ this share of recent frames → masked
    motion_min_frames: int = 4  # history needed before the mask is used
    pip_max_frac: float = 0.35  # a mask bigger than this is not a PiP, it's the content itself
    video_frac: float = 0.6     # moving area bigger than this → full-screen video / camera, skip
    camera_edges: float = 0.015  # something moves + few edges elsewhere → a camera view, not a screen
    revisit_depth: int = 12     # how many recent scenes to check for "back to an earlier slide"


# ── Image helpers ────────────────────────────────────────────────────────────

def prep(image: np.ndarray | None = None, path: str | None = None, width: int = 256) -> np.ndarray | None:
    """Small blurred grayscale copy used for all comparisons."""
    if image is None:
        # Decode at 1/4 size straight away — a Retina JPEG is ~3456 px wide.
        image = cv2.imread(path, cv2.IMREAD_REDUCED_GRAYSCALE_4)
        if image is None:
            return None
    elif image.ndim == 3:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    h, w = image.shape[:2]
    small = cv2.resize(image, (width, max(1, round(h * width / w))), interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(small, (3, 3), 0)


def changed_map(a: np.ndarray, b: np.ndarray, thresh: int) -> np.ndarray | None:
    if a.shape != b.shape:
        return None
    return cv2.absdiff(a, b) > thresh


def frac(moved: np.ndarray | None, mask: np.ndarray | None = None) -> float:
    """Share of changed pixels outside the mask (None = shapes differ = everything changed)."""
    if moved is None:
        return 1.0
    if mask is None:
        return float(moved.mean())
    keep = ~mask
    n = int(keep.sum())
    return float((moved & keep).sum() / n) if n else 0.0


def area(moved: np.ndarray | None, mask: np.ndarray | None = None) -> float:
    """Like frac(), but counts the area the changes touch (5×5 dilation): a line of thin
    text strokes weighs as the block it occupies. Used for scene novelty."""
    if moved is None:
        return 1.0
    return frac(cv2.dilate(moved.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0, mask)


def content_mask(small: np.ndarray) -> np.ndarray:
    """What's drawn on the slide: pixels clearly off the background (the most common level)."""
    bg = int(np.median(small))
    return cv2.dilate((cv2.absdiff(small, np.full_like(small, bg)) > 25).astype(np.uint8),
                      np.ones((5, 5), np.uint8)) > 0


def content_change(a: np.ndarray, b: np.ndarray, thresh: int, mask: np.ndarray | None) -> float:
    """Share of the slides' content (union of both) that changed — 1.0 if shapes differ."""
    moved = changed_map(a, b, thresh)
    if moved is None:
        return 1.0
    content = content_mask(a) | content_mask(b)
    if mask is not None:
        content &= ~mask
    n = int(content.sum())
    if n == 0:
        return 0.0
    moved = cv2.dilate(moved.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    return float((moved & content).sum() / n)


def edge_density(small: np.ndarray) -> float:
    """Share of edge pixels — text-heavy screens score high, camera pictures low."""
    return float((cv2.Canny(small, 60, 160) > 0).mean())


def vertical_shift(a: np.ndarray, b: np.ndarray, thresh: int) -> tuple[int, float] | None:
    """If b is a vertically scrolled a, return (dy, residual change on the overlap)."""
    if a.shape != b.shape:
        return None
    (dx, dy), resp = cv2.phaseCorrelate(a.astype(np.float32), b.astype(np.float32))
    dy = int(round(dy))
    if resp < 0.2 or abs(dx) > 1.5 or abs(dy) < 3 or abs(dy) > a.shape[0] // 2:
        return None
    if dy > 0:
        moved = cv2.absdiff(a[:-dy], b[dy:]) > thresh
    else:
        moved = cv2.absdiff(a[-dy:], b[:dy]) > thresh
    return dy, float(moved.mean())


# ── Scenes ───────────────────────────────────────────────────────────────────

@dataclass
class Scene:
    id: int
    path: str                       # JPEG of the first stable state
    t_start: float                  # seconds since session start (when the screen settled)
    t_end: float                    # last time it was seen on screen
    wall_start: str = ""            # ISO wall-clock time of t_start
    wall_end: str = ""
    changed: float = 0.0            # difference from the previous scene (0..1)
    edges: float = 0.0              # edge density (text-heavy screens are high)
    mask_frac: float = 0.0          # share of the frame masked as moving (PiP)
    last_path: str = ""             # later state of the same scene (form filled in…), if it drifted
    same_as: int | None = None      # this is a revisit of an earlier scene (no new image)
    scroll_of: int | None = None    # looks like a scrolled version of this scene
    source: str = "auto"
    # Level 1 / Level 2 fill these
    cls: str = ""                   # slide | code | browser | face | desktop | diagram …
    cls_score: float = 0.0
    ocr: str = ""
    caption: str = ""
    title: str = ""
    extra: dict = field(default_factory=dict)


class SceneStore:
    """scenes.json + JPEGs in one folder. Thread-safe; rewrites the JSON on every change."""

    def __init__(self, out_dir: Path, session_start: datetime | None = None):
        self.dir = Path(out_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.json_path = self.dir / "scenes.json"
        self.session_start = session_start or datetime.now()
        self.lock = threading.RLock()
        self.scenes: list[Scene] = []
        if self.json_path.exists():   # continued session: keep earlier scenes, ids go on
            try:
                for d in json.loads(self.json_path.read_text(encoding="utf-8"))["scenes"]:
                    self.scenes.append(Scene(**d))
            except Exception as e:
                print(f"[frames] could not read {self.json_path}: {e}", file=sys.stderr)
        self.next_id = max((s.id for s in self.scenes), default=0) + 1

    def wall(self, t: float) -> str:
        return (self.session_start + timedelta(seconds=t)).isoformat(timespec="milliseconds")

    def keep_image(self, t: float, src_path: str | None, image: np.ndarray | None, tag: str = "") -> str:
        name = f"frame_{(self.session_start + timedelta(seconds=t)):%Y%m%d_%H%M%S_%f}"[:-3] + tag + ".jpg"
        dst = self.dir / name
        if src_path:
            shutil.move(src_path, dst)
        else:
            cv2.imwrite(str(dst), image, [cv2.IMWRITE_JPEG_QUALITY, 90])
        return str(dst.resolve())

    def add(self, **kw) -> Scene:
        with self.lock:
            s = Scene(id=self.next_id, **kw)
            self.next_id += 1
            self.scenes.append(s)
            self.save()
            return s

    def update(self, scene: Scene, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(scene, k, v)
            self.save()

    def save(self):
        with self.lock:
            data = {"session_start": self.session_start.isoformat(),
                    "scenes": [asdict(s) for s in self.scenes]}
            tmp = self.json_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.json_path)


# ── Plug-in points for Level 1 / Level 2 ─────────────────────────────────────

class FrameFilter:
    """Level 1: runs synchronously on each Level-0 candidate (≤ 1 per second).
    Return None to keep the candidate, or a reason string to drop it. May set fields
    on *info* (cls, cls_score, extra) that end up in the scene."""
    def check(self, small: np.ndarray, path: str | None, image: np.ndarray | None,
              info: dict) -> str | None:
        """May also set info["dup_of"] = scene id: the candidate is that scene again."""
        return None

    def accepted(self, scene: "Scene", info: dict):
        """Called when the candidate became a new scene."""


class Enricher:
    """Level 2: runs on a background thread for every new scene; returns fields to set
    (ocr, caption, title, cls…)."""
    def enrich(self, scene: Scene) -> dict:
        return {}


# ── Level 0 triage ───────────────────────────────────────────────────────────

class FrameTriage:
    def __init__(self, store: SceneStore, params: Params | None = None,
                 filters: list[FrameFilter] | None = None, enrichers: list[Enricher] | None = None,
                 on_scene=None, on_stats=None):
        self.p = params or Params()
        self.store = store
        self.filters = filters or []
        self.enrichers = enrichers or []
        self.on_scene = on_scene        # callback(scene, kind) kind: new | revisit | drift | extend
        self.on_stats = on_stats        # callback(stats dict)
        self.prev: np.ndarray | None = None
        self.still_since: float | None = None
        self.evaluated = False          # current still period already turned into a decision
        self.history: deque = deque()   # (t, moved map)
        self.current: Scene | None = None     # last accepted scene (novelty reference)
        self.on_screen: Scene | None = None   # scene currently shown (None after a dropped candidate)
        self.current_small: np.ndarray | None = None   # latest state of the current scene
        self.first_small: np.ndarray | None = None     # its first state: drift is measured from here too
        self.recent: deque = deque(maxlen=self.p.revisit_depth)   # (scene, small)
        self.stats = {"frames": 0, "candidates": 0, "scenes": 0, "revisits": 0, "drifts": 0,
                      "dropped": 0, "video": 0, "state": "idle", "mask": 0.0}
        self._enrich_q: queue.Queue = queue.Queue()
        if self.enrichers:
            threading.Thread(target=self._enrich_loop, daemon=True).start()

    # -- motion mask --
    def _mask(self, t: float) -> tuple[np.ndarray | None, float]:
        while self.history and t - self.history[0][0] > self.p.motion_window_sec:
            self.history.popleft()
        maps = [m for _, m in self.history if m is not None]
        if len(maps) < self.p.motion_min_frames or len({m.shape for m in maps}) > 1:
            return None, 0.0
        # Moved often AND lately: a region that stopped moving (the camera view before a
        # switch to slides) leaves the mask at once instead of after the whole window.
        hot = (np.mean(maps, axis=0) >= self.p.motion_hot) & np.any(maps[-3:], axis=0)
        if not hot.any():
            return None, 0.0
        # A webcam PiP is a rectangle and the head moves around inside it: mask the
        # bounding box of each moving blob, not just the pixels that happened to move.
        hot = cv2.dilate(hot.astype(np.uint8), np.ones((7, 7), np.uint8))
        n, _, rects, _ = cv2.connectedComponentsWithStats(hot)
        mask = np.zeros(hot.shape, bool)
        min_area = 0.002 * hot.size
        for x, y, w, h, a in rects[1:n]:
            if a >= min_area:
                mask[max(0, y - 3):y + h + 3, max(0, x - 3):x + w + 3] = True
        return (mask, float(mask.mean())) if mask.any() else (None, 0.0)

    # -- main entry --
    def feed(self, t: float, path: str | None = None, image: np.ndarray | None = None):
        """One captured frame at session time *t*. Either a file (moved into the store if
        kept, deleted otherwise) or an in-memory BGR image (offline mode)."""
        keep_file = False
        try:
            keep_file = self._feed(t, path, image)
        finally:
            if path and not keep_file:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def _feed(self, t, path, image) -> bool:
        small = prep(image, path, self.p.width)
        if small is None:
            return False
        self.stats["frames"] += 1
        prev, self.prev = self.prev, small
        if prev is None:
            self.still_since, self.evaluated = t, False
            self._stats()
            return False

        moved = changed_map(prev, small, self.p.pix_thresh)
        self.history.append((t, moved))
        mask, mask_frac = self._mask(t)
        if mask_frac > self.p.video_frac:
            self.stats["state"] = "video"
            self.stats["video"] += 1
            self.still_since, self.evaluated = None, False
            self._stats(mask_frac)
            return False
        if mask_frac > self.p.pip_max_frac:
            mask = None   # too big for a PiP: whatever moves there is the content

        if frac(moved, mask) > self.p.still_frac:
            self.still_since, self.evaluated = t, False
            self.stats["state"] = "moving"
            self._stats(mask_frac)
            return False
        if self.still_since is None:
            self.still_since = t
        if self.on_screen is not None and self.evaluated:
            self.on_screen.t_end = t        # cheap; persisted on the next save
            self.on_screen.wall_end = self.store.wall(t)
        if self.evaluated or t - self.still_since < self.p.stable_sec:
            self.stats["state"] = "still" if self.evaluated else "settling"
            self._stats(mask_frac)
            return False

        self.evaluated = True
        self.stats["candidates"] += 1
        self.on_screen = None
        kept = self._decide(t, small, path, image, mask, mask_frac)
        self._stats(mask_frac)
        return kept

    def _differs(self, ref: np.ndarray, small: np.ndarray, mask) -> tuple[bool, float, float]:
        """(is a different scene, changed area, share of the content that changed)."""
        p = self.p
        novelty = area(changed_map(ref, small, p.pix_thresh), mask)
        rel = content_change(ref, small, p.pix_thresh, mask) if novelty > p.sparse_min else 0.0
        return novelty > p.scene_frac or rel > p.sparse_rel, novelty, rel

    def _decide(self, t, small, path, image, mask, mask_frac) -> bool:
        p = self.p
        t0 = self.still_since
        rel = 0.0
        if self.current is not None:
            new_vs_last, novelty, rel = self._differs(self.current_small, small, mask)
            # Small steps add up (A → A′ → A″): compare with the scene's first state as well,
            # so a slide is never lost by being "drifted" into the next one.
            new_vs_first = False
            if not new_vs_last and self.first_small is not None and self.first_small is not self.current_small:
                new_vs_first, _, rel_first = self._differs(self.first_small, small, mask)
                rel = max(rel, rel_first)
            if not (new_vs_last or new_vs_first):
                if novelty > p.drift_frac:
                    # Same scene, but it has changed a bit (form filled in, cursor moved a lot…):
                    # keep the latest such state as the scene's "last" image.
                    return self._drift(t, t0, small, path, image)
                self.store.update(self.current, t_end=t, wall_end=self.store.wall(t))
                self.on_screen = self.current
                self._notify(self.current, "extend")
                return False
        else:
            novelty = 1.0

        # Back to something shown a little earlier? Record the interval, don't store the image again.
        for sc, sm in reversed(self.recent):
            if sc is self.current:
                continue
            if not self._differs(sm, small, mask)[0]:
                return self._revisit(sc, t, t0, small, novelty, mask_frac)

        edges = edge_density(small)
        if mask is not None and edges < p.camera_edges:
            self.stats["dropped"] += 1
            self.stats["last_drop"] = "camera"
            return False

        info: dict = {}
        for flt in self.filters:
            reason = flt.check(small, path, image, info)
            if reason:
                self.stats["dropped"] += 1
                self.stats["last_drop"] = reason
                return False

        dup = info.get("dup_of")
        if dup is not None and rel > p.sparse_rel:
            dup = None     # the text changed: look-alike to CLIP (dark title slides), but a new slide
        if dup is not None:
            # A filter says the content is a scene we already have (pixels moved, meaning didn't).
            self.stats["semantic_dups"] = self.stats.get("semantic_dups", 0) + 1
            cur = self.current
            if cur is not None and dup in (cur.id, cur.same_as):
                return self._drift(t, t0, small, path, image)
            orig = next((sc for sc, _ in self.recent if sc.id == dup), None)
            if orig is not None:
                return self._revisit(orig, t, t0, small, novelty, mask_frac)

        scroll_of = None
        if self.current is not None:
            vs = vertical_shift(self.current_small, small, p.pix_thresh)
            if vs and vs[1] <= p.scene_frac:
                scroll_of = self.current.same_as or self.current.id

        kept_path = self.store.keep_image(t0, path, image)
        s = self.store.add(path=kept_path, t_start=t0, t_end=t, wall_start=self.store.wall(t0),
                           wall_end=self.store.wall(t), changed=round(novelty, 4),
                           edges=round(edges, 4), mask_frac=round(mask_frac, 3),
                           scroll_of=scroll_of, cls=info.get("cls", ""),
                           cls_score=info.get("cls_score", 0.0), extra=info.get("extra", {}))
        self.current = self.on_screen = s
        self.current_small = self.first_small = small
        self.recent.append((s, small))
        self.stats["scenes"] += 1
        for flt in self.filters:
            flt.accepted(s, info)
        self._notify(s, "new")
        if self.enrichers:
            self._enrich_q.put(s)
        return True

    def _drift(self, t, t0, small, path, image) -> bool:
        """The current scene changed a bit: keep this state as its "last" image."""
        old = self.current.last_path
        lp = self.store.keep_image(t0, path, image, "_last")
        self.store.update(self.current, last_path=lp, t_end=t, wall_end=self.store.wall(t))
        if old and old != lp:
            try:
                os.unlink(old)
            except OSError:
                pass
        self.current_small = small
        self.on_screen = self.current
        self.stats["drifts"] += 1
        self._notify(self.current, "drift")
        return True

    def _revisit(self, sc: Scene, t, t0, small, novelty, mask_frac) -> bool:
        """Back to an earlier scene: record the interval, don't store the image again."""
        orig = sc.same_as or sc.id
        s = self.store.add(path=sc.path, t_start=t0, t_end=t, wall_start=self.store.wall(t0),
                           wall_end=self.store.wall(t), changed=round(novelty, 4),
                           edges=sc.edges, mask_frac=round(mask_frac, 3), same_as=orig,
                           cls=sc.cls, cls_score=sc.cls_score, ocr=sc.ocr, caption=sc.caption,
                           title=sc.title)
        self.current = self.on_screen = s
        self.current_small = self.first_small = small
        self.recent.append((s, small))
        self.stats["revisits"] += 1
        self._notify(s, "revisit")
        return False

    def _notify(self, scene: Scene, kind: str):
        if self.on_scene:
            try:
                self.on_scene(scene, kind)
            except Exception as e:
                print(f"[frames] on_scene error: {e}", file=sys.stderr)

    def _stats(self, mask_frac: float = 0.0):
        self.stats["mask"] = round(mask_frac, 3)
        for flt in self.filters:
            st = getattr(flt, "stats", None)
            if st:
                self.stats["l1"] = {**st, "ready": flt.ready.is_set()} if hasattr(flt, "ready") else st
        if self.on_stats:
            try:
                self.on_stats(dict(self.stats))
            except Exception:
                pass

    def _enrich_loop(self):
        while True:
            s = self._enrich_q.get()
            if s is None:
                break
            for e in self.enrichers:
                try:
                    fields = e.enrich(s)
                    if fields:
                        self.store.update(s, **fields)
                        self._notify(s, "enriched")
                except Exception as ex:
                    print(f"[frames] enrich error: {ex}", file=sys.stderr)

    def close(self):
        self.store.save()
        if self.enrichers:
            self._enrich_q.put(None)


def level1_filters(enabled: bool) -> list[FrameFilter]:
    """Level 1 (frames_l1.py, CLIP) if enabled and importable; else nothing."""
    if not enabled:
        return []
    try:
        from frames_l1 import ClipFilter
    except Exception as e:
        print(f"[frames] Level 1 unavailable: {e}", file=sys.stderr)
        return []
    return [ClipFilter()]


# ── Live wiring (used by main.py) ────────────────────────────────────────────

class LiveFrames:
    """Receives FRAME:<path> lines from screenshot_helper and triages them on a thread."""

    def __init__(self, out_dir: Path, session_start: datetime, on_scene=None, on_stats=None,
                 params: Params | None = None, level1: bool = True):
        self.session_start = session_start
        self.store = SceneStore(out_dir, session_start)
        self.triage = FrameTriage(self.store, params, filters=level1_filters(level1),
                                  on_scene=on_scene, on_stats=on_stats)
        self.q: queue.Queue = queue.Queue()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def put(self, path: str):
        self.q.put((time.time(), path))

    def _loop(self):
        while True:
            item = self.q.get()
            if item is None:
                break
            ts, path = item
            # Falling behind (e.g. CPU busy with ASR): skip stale frames, keep the newest.
            if self.q.qsize() > 3:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            t = ts - self.session_start.timestamp()
            try:
                self.triage.feed(t, path=path)
            except Exception as e:
                print(f"[frames] triage error: {e}", file=sys.stderr)

    def close(self):
        self.q.put(None)
        self.thread.join(timeout=10)
        self.triage.close()


# ── Offline mode: run on a video file ────────────────────────────────────────

def run_video(video: Path, out_dir: Path, fps: float, params: Params, start_time: datetime | None,
              level1: bool = True):
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise SystemExit(f"Cannot open {video}")
    vfps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    step = max(1, round(vfps / fps))
    store = SceneStore(out_dir, start_time or datetime.fromtimestamp(video.stat().st_mtime))
    filters = level1_filters(level1)
    for f in filters:
        if hasattr(f, "ready"):
            f.ready.wait(300)    # offline: wait for the model instead of passing frames through
    triage = FrameTriage(store, params, filters=filters)
    t0 = time.time()
    idx = 0
    while True:
        if not cap.grab():
            break
        if idx % step == 0:
            ok, frame = cap.retrieve()
            if ok:
                triage.feed(idx / vfps, image=frame)
                if triage.stats["frames"] % 60 == 0:
                    pct = f"{100 * idx / total:.0f}%" if total else ""
                    print(f"\r  {idx / vfps / 60:5.1f} min {pct}  scenes={triage.stats['scenes']}",
                          end="", file=sys.stderr)
        idx += 1
    cap.release()
    triage.close()
    dur = idx / vfps
    print(f"\r\033[K{video.name}: {dur / 60:.1f} min, {triage.stats['frames']} frames → "
          f"{triage.stats['scenes']} scenes (+{triage.stats['revisits']} revisits, "
          f"{triage.stats['drifts']} drifts, {triage.stats['dropped']} dropped, "
          f"{triage.stats.get('semantic_dups', 0)} semantic dups) in {time.time() - t0:.0f}s", file=sys.stderr)
    return store


def evaluate(store: SceneStore, truth_dir: Path, params: Params):
    """Recall of hand-made screenshots: each counts as found if some scene image matches it."""
    truth = sorted(p for p in truth_dir.glob("*.png") if not p.name.startswith("."))
    images = {}
    for s in store.scenes:
        for pth in (s.path, s.last_path):
            if pth and pth not in images:
                images[pth] = prep(path=pth, width=params.width)
    found = 0
    rows = []
    for tp in truth:
        ts = prep(path=str(tp), width=params.width)
        best, best_path = 1.0, ""
        for pth, im in images.items():
            if im is None or ts is None:
                continue
            if im.shape != ts.shape:
                im = cv2.resize(im, (ts.shape[1], ts.shape[0]), interpolation=cv2.INTER_AREA)
            d = area(changed_map(im, ts, params.pix_thresh))
            if d < best:
                best, best_path = d, pth
        hit = best <= params.scene_frac * 2.5
        found += hit
        rows.append((tp.name, best, Path(best_path).name if best_path else "", hit))
    for name, d, m, hit in rows:
        print(f"  {'✓' if hit else '✗'} {name:40s} best diff {d:.3f}  {m}")
    if truth:
        print(f"recall {found}/{len(truth)} = {found / len(truth):.0%}; scenes {len(store.scenes)}")


def write_contact_sheet(store: SceneStore):
    """index.html with every scene, for eyeballing threshold changes."""
    def mmss(t):
        return f"{int(t // 60):02d}:{int(t % 60):02d}"
    cards = []
    for s in store.scenes:
        tags = []
        if s.same_as:
            tags.append(f"revisit of #{s.same_as}")
        if s.scroll_of:
            tags.append(f"scroll of #{s.scroll_of}")
        if s.last_path:
            tags.append("drifted")
        if s.mask_frac:
            tags.append(f"mask {s.mask_frac:.0%}")
        if s.cls:
            tags.append(f"{s.cls} {s.cls_score:.0%}")
        img = Path(s.path).name
        last = f'<img src="{Path(s.last_path).name}" loading="lazy">' if s.last_path else ""
        cards.append(f'<figure><img src="{img}" loading="lazy">{last}<figcaption><b>#{s.id}</b> '
                     f'{mmss(s.t_start)}–{mmss(s.t_end)} · Δ{s.changed:.0%} · edges {s.edges:.2f}'
                     f'<br>{" · ".join(tags)}</figcaption></figure>')
    html = f"""<!doctype html><meta charset="utf-8"><title>Scenes</title>
<style>body{{font:13px system-ui;margin:16px;background:#111;color:#ddd}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}}
figure{{margin:0;background:#1c1c1c;padding:6px;border-radius:6px}}img{{width:100%;display:block}}
figure img+img{{margin-top:4px;opacity:.85}}figcaption{{padding:4px 2px}}</style>
<h3>{len(store.scenes)} scenes</h3><main>{''.join(cards)}</main>"""
    (store.dir / "index.html").write_text(html, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Level-0 auto frames on a video file (offline tuning)")
    ap.add_argument("video", type=Path)
    ap.add_argument("--out", type=Path, required=True, help="Output folder (scenes.json, JPEGs, index.html)")
    ap.add_argument("--fps", type=float, default=1.0, help="Sampling rate (default 1 frame/s, like live)")
    ap.add_argument("--truth", type=Path, default=None, help="Folder of hand-made screenshots to measure recall")
    ap.add_argument("--start", default=None, help="Wall-clock start of the video (ISO), for scene timestamps")
    ap.add_argument("--no-l1", action="store_true", help="Level 0 only (no CLIP filter)")
    for f in Params.__dataclass_fields__.values():
        ap.add_argument("--" + f.name.replace("_", "-"), type=type(f.default), default=f.default)
    a = ap.parse_args()
    params = Params(**{k: getattr(a, k) for k in Params.__dataclass_fields__})
    if a.out.exists() and (a.out / "scenes.json").exists():
        shutil.rmtree(a.out)
    store = run_video(a.video, a.out, a.fps, params,
                      datetime.fromisoformat(a.start) if a.start else None, level1=not a.no_l1)
    write_contact_sheet(store)
    print(f"Scenes: {store.json_path}\nContact sheet: {store.dir / 'index.html'}")
    if a.truth:
        evaluate(store, a.truth, params)


if __name__ == "__main__":
    main()
