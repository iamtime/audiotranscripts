"""Level 1 of the auto-frames cascade: image embeddings (CLIP).

Runs on Level-0 candidates only (a few per minute), as an autoframes.FrameFilter:
  - zero-shot class of the frame (slide, diagram, code, web, face…); a frame that is
    mostly a face / webcam / video call is dropped;
  - semantic dedup: a candidate whose embedding is very close to a recent scene is the
    same scene (a highlight, a tooltip, a popup moved pixels but not the content).

Optional and self-contained: autoframes works without it (main.py --no-frames-l1,
web UI setting "frames_l1"); if the model can't be loaded the filter just passes
everything through.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque

import numpy as np

from autoframes import FrameFilter

DEFAULT_MODEL = "openai/clip-vit-base-patch32"

# class → prompts; a class's probability is the sum over its prompts
CLASSES = {
    "slide": ["a presentation slide with a title and bullet points", "a title slide of a presentation"],
    "diagram": ["a diagram with boxes and arrows", "a chart or a graph", "a software architecture diagram"],
    "table": ["a table or a spreadsheet with numbers"],
    "code": ["source code in a code editor", "a terminal window with text"],
    "web": ["a web page in a browser", "a software application user interface"],
    "document": ["a document with paragraphs of text"],
    "face": ["a close-up of a person's face", "a person talking to a webcam", "a video call with people's faces"],
    "desktop": ["an empty computer desktop with a wallpaper"],
}


class ClipFilter(FrameFilter):
    def __init__(self, model_name: str = DEFAULT_MODEL, face_drop: float = 0.6, dup_sim: float = 0.92,
                 near_sim: float = 0.85, depth: int = 12, background: bool = True, device: str | None = None):
        self.model_name = model_name
        self.face_drop = face_drop      # p(face) at or above → drop the candidate
        # cosine at or above → same as that recent scene. On a slide talk (CLIP B/32) different
        # slides were ≤ 0.84 apart and the same slide later (laser pointer, webcam) ≥ 0.966.
        self.dup_sim = dup_sim
        # between near_sim and dup_sim: kept as its own scene but marked as looking like the
        # other one (a tooltip over a dense slide scored 0.89), so the picker takes only one.
        self.near_sim = near_sim
        self.recent: deque = deque(maxlen=depth)   # (scene_id, embedding)
        self.device = device
        self.ready = threading.Event()
        self.failed = None
        self.stats = {"checked": 0, "faces": 0, "dups": 0, "ms": 0.0}
        if background:
            threading.Thread(target=self._load, daemon=True).start()
        else:
            self._load()

    # -- model --
    def _load(self):
        try:
            import torch
            from transformers import CLIPModel, CLIPProcessor
            t0 = time.time()
            self.torch = torch
            if self.device is None:
                self.device = "mps" if torch.backends.mps.is_available() else "cpu"
            self.model = CLIPModel.from_pretrained(self.model_name).to(self.device).eval()
            self.proc = CLIPProcessor.from_pretrained(self.model_name)
            prompts, owners = [], []
            for cls, ps in CLASSES.items():
                prompts += ps
                owners += [cls] * len(ps)
            with torch.no_grad():
                t = self.proc(text=prompts, return_tensors="pt", padding=True).to(self.device)
                te = self.model.get_text_features(**t)
            self.text = te / te.norm(dim=-1, keepdim=True)
            self.owners = owners
            self.ready.set()
            print(f"[frames] Level 1 ready: {self.model_name} on {self.device} ({time.time() - t0:.1f}s)",
                  file=sys.stderr)
        except Exception as e:
            self.failed = str(e)
            print(f"[frames] Level 1 disabled, model failed to load: {e}", file=sys.stderr)

    def embed(self, image_bgr: np.ndarray) -> tuple[np.ndarray, dict]:
        """(unit embedding, {class: probability})."""
        import cv2
        torch = self.torch
        rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        with torch.no_grad():
            px = self.proc(images=rgb, return_tensors="pt").to(self.device)
            ie = self.model.get_image_features(**px)
            ie = ie / ie.norm(dim=-1, keepdim=True)
            logits = (self.model.logit_scale.exp() * ie @ self.text.T).softmax(dim=-1)[0].cpu().numpy()
        probs: dict[str, float] = {}
        for p, cls in zip(logits, self.owners):
            probs[cls] = probs.get(cls, 0.0) + float(p)
        return ie[0].cpu().numpy(), probs

    # -- FrameFilter --
    def check(self, small, path, image, info) -> str | None:
        if not self.ready.is_set():
            return None                 # still loading (or failed): pass through
        import cv2
        if image is None:
            image = cv2.imread(path)
            if image is None:
                return None
        t0 = time.time()
        emb, probs = self.embed(image)
        self.stats["checked"] += 1
        self.stats["ms"] = round((time.time() - t0) * 1000, 1)
        cls = max(probs, key=probs.get)
        info["cls"], info["cls_score"] = cls, round(probs[cls], 3)
        info.setdefault("extra", {})["l1_probs"] = {k: round(v, 3) for k, v in
                                                    sorted(probs.items(), key=lambda kv: -kv[1])[:3]}
        info["_embedding"] = emb
        if probs.get("face", 0.0) >= self.face_drop:
            self.stats["faces"] += 1
            return "face"
        best_id, best_sim = None, 0.0
        for sid, e in self.recent:
            sim = float(emb @ e)
            if sim > best_sim:
                best_id, best_sim = sid, sim
        info["extra"]["l1_sim"] = round(best_sim, 4)
        if best_id is not None and best_sim >= self.near_sim:
            info["extra"]["l1_near"] = best_id
        if best_id is not None and best_sim >= self.dup_sim:
            self.stats["dups"] += 1
            info["dup_of"] = best_id
        return None

    def accepted(self, scene, info):
        emb = info.get("_embedding")
        if emb is not None:
            self.recent.append((scene.id, emb))
            try:   # cached for "hide this and similar" in the UI (similar.py)
                from similar import save_embedding
                save_embedding(scene.path, emb)
            except Exception:
                pass
