"""External transcripts of the same meeting (e.g. Microsoft Teams), stored per project.

They carry reliable speaker names but often garble brands and terms, so Beautify uses them
to attribute speakers and recover missed words, while our own transcript wins on terms.
Files live in projects/<p>/external/: the uploaded original, a normalized <stem>.transcript.txt
the LLM reads, and index.json binding each file to the session it belongs to.
"""

import html
import io
import re
import zipfile
from datetime import datetime
from pathlib import Path

import sessions as S

ALLOWED = {".vtt", ".docx", ".txt", ".srt", ".md"}
MAX_BYTES = 20 * 1024 * 1024

VTT_TIME_RE = re.compile(r"^(\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}\s*-->\s*")
VOICE_RE = re.compile(r"<v(?:\.[^ >]*)?\s+([^>]+)>")
# Teams .docx/.txt: a speaker line "Jane Doe   0:03" (or "0:0:3.14 --> ..." blocks, handled by the LLM)
SPEAKER_LINE_RE = re.compile(r"^([^\W\d][^\n:]{0,60}?)\s+\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\s*$")


def ext_dir(project: str) -> Path:
    return S.project_dir(project) / "external"


def _index_path(project: str) -> Path:
    return ext_dir(project) / "index.json"


def load_index(project: str) -> list[dict]:
    return S._read_json(_index_path(project), {"files": []})["files"]


def _save_index(project: str, files: list[dict]):
    S._write_json(_index_path(project), {"files": files})


def _hms(ts: str) -> str:
    parts = ts.replace(",", ".").split(".")[0].split(":")
    parts = ["0"] * (3 - len(parts)) + parts
    return ":".join(f"{int(p):02d}" for p in parts)


def _from_vtt(text: str) -> str:
    """Cues → '[HH:MM:SS] Speaker: text', merging consecutive cues of the same speaker."""
    turns: list[list[str]] = []   # [time, speaker, text]
    time = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line == "WEBVTT" or line.startswith(("NOTE", "STYLE", "REGION")):
            continue
        if VTT_TIME_RE.match(line):
            time = _hms(line.split("-->")[0].strip())
            continue
        if time is None or re.fullmatch(r"[0-9a-f-]{8,}(/\d+-\d+)?|\d+", line):   # cue ids
            continue
        m = VOICE_RE.search(line)
        speaker = m.group(1).strip() if m else ""
        body = html.unescape(re.sub(r"</?[^>]+>", "", line)).strip()
        if not body:
            continue
        if turns and turns[-1][1] == speaker and (speaker or turns[-1][0] == time):
            turns[-1][2] += " " + body
        else:
            turns.append([time, speaker, body])
    return "".join(f"[{t}] {s + ': ' if s else ''}{b}\n" for t, s, b in turns)


def _from_docx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if z.getinfo("word/document.xml").file_size > 10 * MAX_BYTES:    # zip bomb
            raise ValueError("The .docx is too large")
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    paras = []
    for p in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S):
        p = re.sub(r"<w:tab/>", "\t", p)
        p = re.sub(r"<w:br/>", "\n", p)
        paras.append(html.unescape("".join(re.findall(r"<w:t(?: [^>]*)?>([^<]*)</w:t>", p))))
    return "\n".join(paras).strip() + "\n"


def normalize(name: str, data: bytes) -> str:
    suffix = Path(name).suffix.lower()
    if suffix == ".docx":
        return _from_docx(data)
    text = data.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    if suffix == ".vtt" or text.lstrip().startswith("WEBVTT"):
        return _from_vtt(text)
    return text if text.endswith("\n") else text + "\n"


def speakers(text: str) -> list[str]:
    names: dict[str, None] = {}
    for line in text.splitlines():
        m = re.match(r"^\[\d{2}:\d{2}:\d{2}\] ([^:\[\]]{2,60}): ", line) or SPEAKER_LINE_RE.match(line.strip())
        if m:
            names.setdefault(m.group(1).strip(), None)
    return list(names)[:50]


def add(project: str, filename: str, data: bytes, sid: str | None) -> dict:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED:
        raise ValueError(f"Unsupported file type {suffix or '(none)'}: use .vtt, .docx, .txt or .srt")
    if not data:
        raise ValueError("The file is empty")
    if len(data) > MAX_BYTES:
        raise ValueError("The file is too large")
    text = normalize(filename, data)
    if not text.strip():
        raise ValueError("No text found in the file")
    d = ext_dir(project)
    d.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w.\- ]+", "_", Path(filename).stem).strip() or "transcript"
    files = load_index(project)
    taken = {f["name"] for f in files}
    name, n = stem, 2
    while name in taken:
        name, n = f"{stem} ({n})", n + 1
    (d / f"{name}{suffix}").write_bytes(data)
    (d / f"{name}.transcript.txt").write_text(text, encoding="utf-8")
    rec = {"name": name, "original": f"{name}{suffix}", "text": f"{name}.transcript.txt", "sid": sid,
           "uploaded": datetime.now().isoformat(timespec="seconds"),
           "lines": sum(1 for l in text.splitlines() if l.strip()), "speakers": speakers(text)}
    _save_index(project, files + [rec])
    return rec


def remove(project: str, name: str):
    files = load_index(project)
    rec = next((f for f in files if f["name"] == name), None)
    if not rec:
        raise ValueError("No such external transcript")
    for fn in (rec["original"], rec["text"]):
        (ext_dir(project) / fn).unlink(missing_ok=True)
    _save_index(project, [f for f in files if f["name"] != name])


def set_session(project: str, name: str, sid: str | None) -> dict:
    files = load_index(project)
    rec = next((f for f in files if f["name"] == name), None)
    if not rec:
        raise ValueError("No such external transcript")
    rec["sid"] = sid
    _save_index(project, files)
    return rec


def for_session(project: str, sid: str) -> list[Path]:
    """Normalized texts that apply to this session (bound to it, or to every session)."""
    return [p for f in load_index(project) if f.get("sid") in (None, sid)
            for p in [ext_dir(project) / f["text"]] if p.exists()]


def public(project: str) -> list[dict]:
    return [{**f, "path": str(ext_dir(project) / f["text"])} for f in load_index(project)]
