#!/usr/bin/env python3
"""Pick the auto frames that belong in the text: keep the best N visible, hide the rest.

Which frame matters is decided by the transcript, not the picture: what was being said while
the scene was on screen. Per scene (revisits of the same image are pooled) we compute
  deixis    "вот здесь", "как видите", "on this slide"… — the speaker points at the screen
  duration  how long it was shown
  words     how much was said meanwhile
  novelty   how different it was from the previous scene
and keep the top N. Planned next: OCR-word overlap with the speech (Level 2) and a final
pick by a text LLM given the transcript + scene list.

Every visible frame goes to Beautify as a screenshot at its own time (sessions.beautify_text);
picking hides the frames that didn't make it (sessions/<sid>/visibility.json), and any of them
can be shown again with the eye button in the UI.

  python select_frames.py <project> <sid> [-n 10] [--apply]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import sessions as S

GRACE_SEC = 4.0          # speech right after a scene disappears often still refers to it
MAX_ENTRY_SEC = 30.0     # a transcript line is assumed to cover at most this long

DEIXIS = [
    r"вот\s+(?:здесь|тут|это|эт[аоуи]т?|так|сюда|видно)", r"как\s+(?:вы\s+)?видите", r"как\s+видно",
    r"(?:по)?смотрите", r"видим", r"видите", r"здесь\s+у\s+нас", r"тут\s+у\s+нас", r"сюда",
    r"покажу", r"показываю", r"на\s+(?:этом|этой|экране|слайде|схеме|картинке|диаграмме)",
    r"(?:этот|эта|это)\s+(?:слайд|схема|экран|диаграмма|таблица|график|страница|окно)",
    r"here\s+(?:you\s+)?(?:can\s+)?see", r"as\s+you\s+(?:can\s+)?see", r"look\s+at",
    r"(?:this|that)\s+(?:slide|diagram|screen|page|chart|table|window)", r"over\s+here",
    r"on\s+the\s+screen", r"let\s+me\s+show", r"you\s+can\s+see",
]
DEIXIS_RE = re.compile(r"(?<!\w)(?:" + "|".join(DEIXIS) + r")(?!\w)", re.IGNORECASE)


frames_dir = S.frames_dir
load_scenes = S.load_scenes


def timed_entries(project: str, sid: str) -> list[dict]:
    """Transcript entries with absolute start/end datetimes (lines hidden in the UI left out)."""
    hidden = set(S.load_visibility(project, sid)["lines"])
    entries = [e for e in S.parse_transcript(S.read_transcript(project, sid)) if e["key"] not in hidden]
    day = S.sid_datetime(sid) or datetime.now()
    out, prev = [], None
    for e in entries:
        h, m, s = map(int, e["time"].split(":"))
        dt = day.replace(hour=h, minute=m, second=s, microsecond=0)
        if prev and dt < prev - timedelta(hours=12):   # crossed midnight
            day += timedelta(days=1)
            dt += timedelta(days=1)
        prev = dt
        out.append({**e, "at": dt})
    for i, e in enumerate(out):
        nxt = out[i + 1]["at"] if i + 1 < len(out) else e["at"] + timedelta(seconds=MAX_ENTRY_SEC)
        e["until"] = min(nxt, e["at"] + timedelta(seconds=MAX_ENTRY_SEC))
    return out


def assign_speech(entries, scenes) -> dict[int, list[str]]:
    """Give each speech line to the one scene it overlaps most (scene intervals extended by
    GRACE_SEC), so a line is never counted for two scenes."""
    spans = [(sc["id"], datetime.fromisoformat(sc["wall_start"]),
              datetime.fromisoformat(sc["wall_end"]) + timedelta(seconds=GRACE_SEC)) for sc in scenes]
    out: dict[int, list[str]] = {sc["id"]: [] for sc in scenes}
    for e in entries:
        if e["kind"] != "speech":
            continue
        best, best_ov = None, 0.0
        for sid_, start, end in spans:
            ov = (min(end, e["until"]) - max(start, e["at"])).total_seconds()
            if ov > best_ov:
                best, best_ov = sid_, ov
        if best is not None:
            out[best].append(e["text"])
    return out


def score_scenes(project: str, sid: str) -> list[dict]:
    scenes = load_scenes(project, sid)
    speech = assign_speech(timed_entries(project, sid), scenes)
    groups: dict[int, dict] = {}
    for sc in scenes:
        key = sc.get("same_as") or sc["id"]
        g = groups.setdefault(key, {"id": key, "scene": None, "shows": [], "duration": 0.0,
                                    "deixis": [], "words": 0})
        if sc["id"] == key:
            g["scene"] = sc
        start, end = datetime.fromisoformat(sc["wall_start"]), datetime.fromisoformat(sc["wall_end"])
        text = " ".join(speech[sc["id"]])
        g["shows"].append([sc["wall_start"], sc["wall_end"]])
        g["duration"] += max(0.0, (end - start).total_seconds())
        g["deixis"] += [m.group(0) for m in DEIXIS_RE.finditer(text)]
        g["words"] += len(text.split())
    rows = []
    for g in groups.values():
        sc = g["scene"]
        if sc is None:
            continue
        f = {
            "deixis": len(g["deixis"]),
            "duration": round(g["duration"], 1),
            "words": g["words"],
            "novelty": sc.get("changed", 0),
            "edges": sc.get("edges", 0),
            "shows": len(g["shows"]),
        }
        score = (1.5 * min(f["deixis"], 3)
                 + math.log2(1 + f["duration"] / 10)
                 + min(f["words"] / 80, 1.0)
                 + (0.5 if f["novelty"] >= 0.1 else 0.0)
                 - (1.0 if f["edges"] < 0.02 else 0.0)      # little detail: likely a camera picture
                 - (0.5 if sc.get("scroll_of") else 0.0))
        if sc.get("cls") == "face":                        # Level 1, when present
            score -= 3
        rows.append({"id": sc["id"], "path": sc.get("last_path") or sc["path"], "first_path": sc["path"],
                     "wall_start": sc["wall_start"], "shows": g["shows"], "features": f,
                     "deixis": g["deixis"][:6], "score": round(score, 3)})
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def select(project: str, sid: str, n: int = 10, apply: bool = True) -> dict:
    """Score, mark the top N; with apply, hide every other frame (and show the picked ones)."""
    rows = score_scenes(project, sid)
    # Level 1 marks look-alikes (extra.l1_near); pick at most one of each such pair.
    near = {sc["id"]: sc.get("extra", {}).get("l1_near") for sc in load_scenes(project, sid)}
    picked: set[int] = set()
    for r in rows:
        other = near.get(r["id"])
        twins = {other} | {k for k, v in near.items() if v == r["id"]}
        r["selected"] = len(picked) < n and not (twins & picked)
        if r["selected"]:
            picked.add(r["id"])
        elif twins & picked:
            r["skipped_as_near"] = sorted(twins & picked)
    result = {"sid": sid, "n": n, "created": datetime.now().isoformat(timespec="seconds"),
              "method": "heuristic-v0", "scenes": rows}
    S._write_json(frames_dir(project, sid) / "selection.json", result)
    if apply:
        vis = S.load_visibility(project, sid)
        vis["frames"] = [r["id"] for r in rows if not r["selected"]]
        S.save_visibility(project, sid, vis)
    return result


def frames_payload(project: str, sid: str) -> dict:
    """Scenes (+ the last selection) for the web UI."""
    scenes = load_scenes(project, sid)
    sel_path = frames_dir(project, sid) / "selection.json"
    selection = json.loads(sel_path.read_text(encoding="utf-8")) if sel_path.exists() else None
    scores = {r["id"]: r for r in (selection or {}).get("scenes", [])}
    hidden = set(S.load_visibility(project, sid)["frames"])
    out = []
    for sc in scenes:
        r = scores.get(sc["id"])
        out.append({**sc, "url": S.file_url(sc["path"]), "hidden": (sc.get("same_as") or sc["id"]) in hidden,
                    "last_url": S.file_url(sc["last_path"]) if sc.get("last_path") else None,
                    "score": r["score"] if r else None, "selected": bool(r and r["selected"]),
                    "features": r["features"] if r else None, "deixis": r["deixis"] if r else []})
    return {"scenes": out, "selection": {k: v for k, v in (selection or {}).items() if k != "scenes"} or None}


def main():
    ap = argparse.ArgumentParser(description="Score auto frames against the transcript and pick the top N")
    ap.add_argument("project")
    ap.add_argument("sid")
    ap.add_argument("-n", type=int, default=10)
    ap.add_argument("--apply", action="store_true", help="Hide every frame that wasn't picked")
    a = ap.parse_args()
    res = select(a.project, a.sid, a.n, apply=a.apply)
    if not res["scenes"]:
        sys.exit(f"No scenes in {frames_dir(a.project, a.sid)}")
    for r in res["scenes"]:
        f = r["features"]
        print(f"{'★' if r['selected'] else ' '} #{r['id']:<4} {r['wall_start'][11:19]}  score {r['score']:5.2f}  "
              f"deixis {f['deixis']}  {f['duration']:6.1f}s  {f['words']:4d} words  ×{f['shows']}  "
              f"{', '.join(r['deixis'][:3])}")


if __name__ == "__main__":
    main()
