"""Find the video player inside a captured window, so auto frames can be cropped to it.

When the capture is a whole browser / Teams window, most of it is static page text and the
video is one rectangle. Comparing whole frames then makes slide changes look tiny (a dark
slide with little text changes < 3% of the window) and scenes get merged. Cropping to the
player fixes that.

detect(frames) looks at a few seconds of captures of a PLAYING video:
  1. live pixels: a decoded video flickers by a level or two even on a still slide, while
     a rendered page is bit-for-bit static. The biggest blob of changed pixels (noise plus
     real motion) is the video, or most of it: uniform parts of a slide don't flicker;
  2. if a player border (long straight edges, video aspect ratio) encloses that blob
     snugly, the crop is that rectangle;
  3. otherwise (a dark slide on a dark page has no visible border) the blob is grown to
     the nearest video aspect ratio and padded, so slide titles near the edge stay in.
No live pixels (paused video): a clearly bordered video-shaped rectangle, else None; only a small live area and no border → {"error": …}.
Otherwise a crop in fractions of the image: {"x", "y", "w", "h", "method"}.

  python video_detect.py frame1.png frame2.png …    # prints the crop
"""

from __future__ import annotations

import sys

import cv2
import numpy as np

WIDTH = 640               # analysis width
GRAD = 14                 # brightness step that counts as an edge
ASPECTS = (16 / 9, 4 / 3, 16 / 10)
MIN_SIDE = 0.18           # a candidate side is at least this share of the image
MIN_SUPPORT = 0.6         # mean edge coverage of the four sides
MAX_CANDIDATES = 28


def _prep(img: np.ndarray) -> np.ndarray:
    if img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = img.shape
    img = cv2.resize(img, (WIDTH, max(1, round(h * WIDTH / w))), interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(img, (3, 3), 0).astype(np.int16)


def _runs(line: np.ndarray, min_len: int) -> bool:
    """Does a boolean row contain a run of True at least min_len long?"""
    if line.sum() < min_len:
        return False
    d = np.diff(np.concatenate(([0], line.astype(np.int8), [0])))
    starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    return bool(((ends - starts) >= min_len).any())


def _candidates(edge: np.ndarray, min_len: int, limit: int) -> list[int]:
    """Rows of *edge* holding a long straight run, strongest first, merged within 2 px."""
    scored = [(int(edge[i].sum()), i) for i in range(edge.shape[0]) if _runs(edge[i], min_len)]
    scored.sort(reverse=True)
    out: list[int] = []
    for _, i in scored:
        if all(abs(i - j) > 2 for j in out):
            out.append(i)
        if len(out) >= limit:
            break
    return sorted(out)


def live_blob(images: list[np.ndarray]) -> tuple[int, int, int, int] | None:
    """Bounding box (x0, y0, x1, y1), in the units of the given images, of the sizeable
    blobs of pixels that changed at all between consecutive frames."""
    g = []
    for im in images:
        im = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY) if im.ndim == 3 else im
        h, w = im.shape
        if w > 1280:   # nearest-neighbour keeps the flicker that area averaging would smooth away
            im = cv2.resize(im, (1280, round(h * 1280 / w)), interpolation=cv2.INTER_NEAREST)
        g.append(im.astype(np.int16))
    if len(g) < 2 or len({x.shape for x in g}) > 1:
        return None
    live = np.zeros(g[0].shape, np.uint8)
    for a, b in zip(g, g[1:]):
        live |= (np.abs(a - b) >= 2).astype(np.uint8)
    if live.mean() < 0.001:
        return None
    k = max(3, g[0].shape[1] // 40)
    joined = cv2.dilate(live, np.ones((k, k), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(joined)
    if n < 2:
        return None
    # The page around a player is static, so every sizeable blob is part of the video (a
    # dark slide's text flickers in separate patches, the webcam in another); tiny ones are
    # page animations (spinners, a blinking caret).
    total = int(live.sum())
    keep = np.zeros(live.shape, bool)
    for i in range(1, n):
        part = (lab == i) & (live > 0)
        if part.sum() >= 0.005 * total:
            keep |= part
    ys, xs = np.nonzero(keep)
    sy, sx = images[0].shape[0] / g[0].shape[0], images[0].shape[1] / g[0].shape[1]
    return (int(xs.min() * sx), int(ys.min() * sy), int((xs.max() + 1) * sx), int((ys.max() + 1) * sy))


def bordered_rects(still: np.ndarray):
    """Rectangles bounded by long straight edges, as (support, l, t, r, b) in analysis px."""
    H, W = still.shape
    eh = np.abs(np.diff(still, axis=0)) > GRAD                    # (H-1, W)
    ev = np.abs(np.diff(still, axis=1)) > GRAD                    # (H, W-1)
    eh = cv2.dilate(eh.astype(np.uint8), np.ones((3, 1), np.uint8)) > 0
    ev = cv2.dilate(ev.astype(np.uint8), np.ones((1, 3), np.uint8)) > 0
    rows = sorted(set(_candidates(eh, int(MIN_SIDE * W), MAX_CANDIDATES)) | {0, H - 2})
    cols = sorted(set(_candidates(ev.T, int(MIN_SIDE * H), MAX_CANDIDATES)) | {0, W - 2})
    sh = np.concatenate([np.zeros((eh.shape[0], 1)), np.cumsum(eh, axis=1)], axis=1)
    sv = np.concatenate([np.zeros((1, ev.shape[1])), np.cumsum(ev, axis=0)], axis=0)

    def hsup(y, x0, x1):   # a window edge bounds a player too, but proves nothing on its own
        return 0.75 if y in (0, H - 2) else (sh[y, x1] - sh[y, x0]) / max(1, x1 - x0)

    def vsup(x, y0, y1):
        return 0.75 if x in (0, W - 2) else (sv[y1, x] - sv[y0, x]) / max(1, y1 - y0)

    for ti, t in enumerate(rows):
        for b in rows[ti + 1:]:
            if b - t < MIN_SIDE * H:
                continue
            for l in cols:
                for r in cols:
                    if r - l < MIN_SIDE * W or (t, l, b, r) == (0, 0, H - 2, W - 2):
                        continue
                    sup = (hsup(t, l, r) + hsup(b, l, r) + vsup(l, t, b) + vsup(r, t, b)) / 4
                    if sup >= MIN_SUPPORT:
                        yield sup, l, t, r, b


def _border_only(frames, W, H) -> dict | None:
    """Nothing is changing (paused video): the biggest clearly bordered video-shaped rectangle."""
    still = np.median(np.stack(frames), axis=0).astype(np.int16)
    best, best_score = None, 0.0
    for sup, l, t, r, b in bordered_rects(still):
        ar = (r - l) / (b - t)
        area = (r - l) * (b - t) / (W * H)
        aerr = min(abs(ar - x) / x for x in ASPECTS)
        if sup < 0.85 or area < 0.10 or aerr >= 0.03:
            continue
        score = sup - aerr + 0.2 * area     # the crispest border first, size only breaks ties
        if score > best_score:
            best, best_score = (l, t, r, b), score
    if best is None:
        return None
    l, t, r, b = best
    return {"x": round(l / W, 4), "y": round(t / H, 4), "w": round((r - l) / W, 4),
            "h": round((b - t) / H, 4), "method": "border (paused)"}


def detect(images: list[np.ndarray]) -> dict | None:
    if len({im.shape[:2] for im in images}) > 1:
        return None
    blob = live_blob(images)
    IH, IW = images[0].shape[:2]
    frames = [_prep(im) for im in images]
    H, W = frames[0].shape
    if blob is None:
        return _border_only(frames, W, H)
    s = W / IW
    bx0, by0, bx1, by1 = blob[0] * s, blob[1] * s, blob[2] * s, blob[3] * s
    barea = max(1.0, (bx1 - bx0) * (by1 - by0))

    # 2. a player border that encloses the live blob snugly
    still = np.median(np.stack(frames), axis=0).astype(np.int16)
    best, best_score = None, 0.0
    for sup, l, t, r, b in bordered_rects(still):
        if l > bx0 + 3 or t > by0 + 3 or r < bx1 - 3 or b < by1 - 3:
            continue
        area = (r - l) * (b - t)
        if area > 2.2 * barea:
            continue
        ar = (r - l) / (b - t)
        aspect_bonus = 0.25 if min(abs(ar - x) / x for x in ASPECTS) < 0.03 else 0.0
        score = sup + aspect_bonus - 0.3 * (area / barea - 1)
        if score > best_score:
            best, best_score = (l / W, t / H, r / W, b / H), score
    if best:
        l, t, r, b = best
        method = "border"
    else:
        # 3. no border: grow the blob to the nearest video aspect ratio, then pad
        l, t, r, b = blob[0] / IW, blob[1] / IH, blob[2] / IW, blob[3] / IH
        w, h = (r - l) * IW, (b - t) * IH
        a = min(ASPECTS, key=lambda x: abs(w / h - x))
        if w / h < a:
            w2, h2 = h * a, h
        else:
            w2, h2 = w, w / a
        cx, cy = (l + r) / 2 * IW, (t + b) / 2 * IH
        # Uniform slide areas don't flicker, so the blob can be short of the real edges:
        # pad by 6% of the video's size (extra static page costs nothing).
        w2, h2 = w2 * 1.12, h2 * 1.12
        l = max(0.0, (cx - w2 / 2) / IW)
        r = min(1.0, (cx + w2 / 2) / IW)
        t = max(0.0, (cy - h2 / 2) / IH)
        b = min(1.0, (cy + h2 / 2) / IH)
        method = "activity"
        if (r - l) * (b - t) < 0.10:
            # Only something small moved (typically the speaker's webcam on a still slide):
            # a crop to it would cut the slides away.
            return {"error": "Only a small part of the window was changing (probably the speaker's "
                             "webcam). Wait for the next slide and try again, or select the area by hand."}
    return {"x": round(l, 4), "y": round(t, 4), "w": round(r - l, 4), "h": round(b - t, 4), "method": method}


if __name__ == "__main__":
    ims = [cv2.imread(p) for p in sys.argv[1:]]
    print(detect([im for im in ims if im is not None]))
