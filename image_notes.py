"""What is on the screenshots and auto frames, as text for Beautify.

Each image can have two texts next to it:
  <image>.txt       OCR (EasyOCR, done by the recorder for screenshots and auto frames)
  <image>.desc.md   a description by a vision model (the Describe button): what the image
                    is and everything that matters on it; it replaces the OCR text
Before Beautify, the texts of all the session's images go to the model in one more request
(condense) that drops the boilerplate (menus, toolbars, repeated pages, OCR noise) and keeps
what helps to write the transcript; each part of the transcript then gets the notes of its time.
"""

import base64
import hashlib
import io
import json
import re
from pathlib import Path

import llm_api
import sessions as S

# Providers that take images over their OpenAI-compatible API, and a cheap vision model of each
VISION_MODELS = {
    "openai": "gpt-5-mini",
    "gemini": "gemini-3.5-flash",
    "anthropic": "claude-haiku-4-5",
    "openrouter": "google/gemini-2.5-flash",
}
MAX_SIDE = 1600          # px: a Retina screenshot is downscaled before it is sent
ITEM_CHARS = 2000        # of one image's text in the condense request
BATCH_CHARS = 60000      # of one condense request

DESCRIBE_PROMPT = """This image was captured from the screen during a recorded meeting, lecture or video. \
Describe it for someone who writes an annotated transcript of the recording: they need what the \
speakers were looking at, and the exact spelling of what is written there.

1. First line: what it is (slide, website, app, document, code, chart, diagram, video frame, \
person on camera…) and its title or topic.
2. Then everything meaningful on it: headings, every readable name, product and technical term, \
number, date, table row, code identifier, chart axes and values, diagram labels and how they connect. \
Copy the text exactly as written.
3. Skip the interface around the content (menus, toolbars, tabs, address bars, taskbars, buttons, \
cookie banners, ads) unless it is the subject itself.

Write in the main language of the text on the image. Plain text, short lines or a Markdown list, \
no preamble."""

CONDENSE_PROMPT = """Below is an index of images captured from the screen during a recording: \
screenshots and auto frames, each with its time, file name and its text: a description by a vision \
model, or OCR (OCR is noisy: broken words, interface labels).

Make short notes from it for the writer of an annotated transcript of the recording. They use \
them to spell names and terms right and to know what the speakers were looking at.
- For each image keep only what matters: its title or topic, names, product and technical terms, \
numbers, key statements, code identifiers.
- Remove boilerplate: menus, navigation, toolbars, browser and window chrome, cookie banners, \
footers, ads, legal text, OCR garbage.
- Don't repeat what an earlier image already gave: write "same as HH:MM:SS" or what changed.
- Drop an image entirely when nothing useful is left (a face, a transition, an empty desktop).
- Keep the original language and the exact spelling of names and terms. Never add facts.

Output, for each image you keep: a line "[HH:MM:SS] file name", then 1-6 short lines. Nothing else.

{index}"""


def desc_path(image: str | Path) -> Path:
    return Path(image).with_suffix(".desc.md")


def ocr_path(image: str | Path) -> Path:
    return Path(image).with_suffix(".txt")


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace").strip() if p.exists() else ""
    except OSError:
        return ""


def image_text(*images: str | Path | None) -> tuple[str, str]:
    """(kind, text) of an image: its description when there is one, else its OCR text.
    Several paths = states of one auto frame (the shown one first, then the first state)."""
    paths = [x for x in images if x]
    for p in paths:
        d = _read(desc_path(p))
        if d:
            return "description", d
    for p in paths:
        o = _read(ocr_path(p))
        if o:
            return "OCR", o
    return "", ""


# ── Describe: a vision model ─────────────────────────────────────────────────

def vision_config(settings: dict | None = None) -> dict:
    """Provider settings for Describe: the chosen provider, else the Beautify one when it
    can see images, else the first vision provider with a key."""
    s = settings or S.load_settings()
    provider = s.get("vision_provider") or ""
    if not provider:
        pick = [s.get("api_provider") or ""] + list(VISION_MODELS)
        provider = next((p for p in pick if p in VISION_MODELS and llm_api.api_key(p)), "")
    if not provider:
        return {"provider": "", "base_url": "", "model": "", "key": ""}
    same = provider == s.get("api_provider")
    cfg = llm_api.config({"api_provider": provider, "api_base_url": s.get("api_base_url") if same else "",
                          "api_model": s.get("vision_model") or VISION_MODELS.get(provider, "")})
    return cfg


def vision_problem(cfg: dict) -> str:
    if not cfg["provider"]:
        return ("No model that can see images: add an OpenAI, Gemini, Anthropic or OpenRouter key "
                "in Settings › Screen descriptions")
    return llm_api.problem(cfg)


def _image_data_url(path: Path) -> str:
    """The image as a data: URL, downscaled to MAX_SIDE when Pillow is there."""
    data, mime = path.read_bytes(), "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        if max(im.size) > MAX_SIDE or len(data) > 1_500_000:
            im.thumbnail((MAX_SIDE, MAX_SIDE))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=88)
            data, mime = buf.getvalue(), "image/jpeg"
    except Exception:
        pass
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def describe(image: str | Path, settings: dict | None = None) -> str:
    """Ask the vision model about one image; saves and returns the description."""
    path = Path(image)
    if not path.is_file():
        raise FileNotFoundError(f"No such image: {path.name}")
    cfg = vision_config(settings)
    why = vision_problem(cfg)
    if why:
        raise RuntimeError(why)
    messages = [{"role": "user", "content": [
        {"type": "text", "text": DESCRIBE_PROMPT},
        {"type": "image_url", "image_url": {"url": _image_data_url(path)}},
    ]}]
    try:
        text = llm_api.stream(cfg, messages).strip()
    except Exception as e:
        raise RuntimeError(llm_api.explain(e)) from e
    desc_path(path).write_text(text + "\n", encoding="utf-8")
    return text


# ── Condense: the index of all images → notes ────────────────────────────────

def index_items(shots: list[dict], frames: list[dict]) -> list[dict]:
    """[{time, name, kind, text}] of the screenshots and frames that have any text, by time.
    shots: {path, time}; frames: {path, time, first_path?}."""
    items = []
    for x in shots + frames:
        kind, text = image_text(x["path"], x.get("first_path"))
        if text:
            items.append({"time": x["time"], "name": Path(x["path"]).name, "kind": kind, "text": text})
    return sorted(items, key=lambda i: i["time"])


def _item_block(i: dict) -> str:
    text = i["text"] if len(i["text"]) <= ITEM_CHARS else i["text"][:ITEM_CHARS] + " …"
    return f"--- [{i['time']}] {i['name']} ({i['kind']}) ---\n{text}\n"


def index_key(items: list[dict]) -> str:
    return hashlib.sha1(json.dumps(items, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def condense(items: list[dict], ask, log=None) -> str:
    """Notes for all items. ask(prompt) -> reply; the index goes in batches of BATCH_CHARS."""
    batches, cur = [], ""
    for i in items:
        b = _item_block(i)
        if cur and len(cur) + len(b) > BATCH_CHARS:
            batches.append(cur)
            cur = ""
        cur += b
    if cur:
        batches.append(cur)
    out = []
    for k, batch in enumerate(batches, 1):
        if log:
            log(f"Screen notes {k}/{len(batches)}: {len(batch):,} chars of image text")
        reply = ask(CONDENSE_PROMPT.format(index=batch)).strip()
        reply = re.sub(r"^```\w*\n|```$", "", reply).strip()
        if reply:
            out.append(reply)
    return "\n\n".join(out)


NOTE_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]", re.M)


def notes_between(notes: str, t_from: str, t_to: str) -> str:
    """The notes of images shown from t_from to t_to (a part of the transcript); the image
    on screen when the part starts is included."""
    blocks = [(m.group(1), m.start()) for m in NOTE_RE.finditer(notes)]
    out = []
    for k, (t, pos) in enumerate(blocks):
        end = blocks[k + 1][1] if k + 1 < len(blocks) else len(notes)
        nxt = blocks[k + 1][0] if k + 1 < len(blocks) else "99:99:99"
        if (not t_from or nxt > t_from) and (not t_to or t <= t_to):
            out.append(notes[pos:end].strip())
    return "\n".join(out)
