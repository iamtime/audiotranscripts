#!/usr/bin/env python3
"""Beautify: turn a raw transcript (+ screenshots) into a FULL annotated HTML
transcript - every utterance kept, real speakers identified, SUMMARY callouts per topic.
An update run compares the new part with what is already known instead (NEW / CONFIRMS /
UPDATES callouts, via claude-context MCP when available).

Each run produces a new version in projects/<project>/sessions/<sid>/vN.html.
Modes:
  update  extend the previous version in place with the new part of the transcript
  full    rebuild from scratch
  final   rebuild after the session ended (diarized transcript); also copied to docs/

Used by server.py; can also be run from the console:
  python beautify.py <project> <sid> [--mode full|update|final] [--model opus]
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import claude_cli
import llm_api
import external
from sessions import (
    DATA_ROOT, ROOT, beautify_text, latest_version, load_manifest, load_notes, load_project_settings, load_settings, notes_path,
    parse_transcript, project_dir, save_manifest, session_dir, shot_time,
    sid_datetime, transcript_path,
)

DEFAULT_STYLE = ROOT / "templates" / "annotated_transcript_style.html"
TIME_RE = re.compile(r'class="time"[^>]*>(\d{2}:\d{2}:\d{2})')


PROMPT = """\
Create the {mode_title} of a FULL annotated transcript of a recorded session, as one self-contained HTML file.

## Inputs
- Raw speech-to-text transcript (snapshot): {input}
  {n_lines} lines, {t_first}–{t_last}{live_note}
  Line formats: "[HH:MM:SS] text"; "[HH:MM:SS] [Speaker N] text" (automatic diarization: usually heavily over-split, treat speaker numbers only as weak hints); "[HH:MM:SS] [Me] text" / "[HH:MM:SS] [Them] text" (a call recorded as two channels: Me = the person recording, from their microphone - reliable; Them = everyone else, from the call app's audio; with diarization the Them lines become [Speaker N] next to the [Me] ones); "[HH:MM:SS] [screenshot: /abs/path.png]" (the screen was captured at that moment); "[HH:MM:SS] [selection: text]" (text the presenter highlighted on screen).
- Screenshots taken during the session. View them with the Read tool: they show slides, documents, shared screens and often the meeting's participant list, so use them to identify speakers and to fix names, product names and technical terms. OCR text sits next to each image (.txt) if you need it.
{shots}
- Style reference: {style}
  Copy ONLY its visual style and structure (CSS incl. dark mode, header, "Who's speaking" chips, callout legend, numbered section dividers, speaker turns, callouts, screenshot figures, footer). Never copy its content.
{prev_block}{context_block}{notes_block}{ext_block}{docs_block}
## Requirements
1. Keep EVERY utterance. Do not summarize, shorten, condense or skip anything, including small talk. Merge fragments into clean speaker turns, fix obvious speech-to-text garbles, and keep each speaker's own wording. Keep the original language of the speech (do not translate).
2. Work out the real speakers from content, self-introductions, how people address each other, and the screenshots. Use one consistent name per person. If a voice can't be identified, give it a descriptive label like "Client (ops)". Give each turn the time of its first line.
3. Group the conversation into numbered sections by topic.
4. {callout_rule}
5. Screenshots: place every screenshot near the moment it was taken, as
   <figure class="shot"><img src="RELATIVE_SRC" alt="…" loading="lazy"><figcaption><span class="time">HH:MM:SS</span>what is shown and why it matters</figcaption></figure>
   Use the exact relative src listed for each screenshot above.
6. Header: title, subtitle, date and time range, "Who's speaking" chips, callout legend (only the callout types the document uses), hint. Footer: provenance (source transcript name, number of lines, time range, version {v}, generated {now}) plus notes on speaker attribution and corrected terms.
7. Self-contained HTML with inline CSS, light and dark mode as in the reference, and no external resources except the screenshot images. Write the content as static HTML, with no JavaScript rendering.
8. Output file: {out}
{write_rule}
9. Don't create, modify or delete any other files.
10. When finished, reply with one short line only: identified speakers · sections · callouts · screenshots.
"""

# Built from the transcript alone (full / final): nothing to compare with, so no NEW / CONFIRMS / UPDATES.
CALLOUTS_SUMMARY = """Callouts: after each meaningful topic, add a SUMMARY callout exactly as in the style reference:
   <aside class="callout summary"><div class="top"><span class="badge summary">📝 SUMMARY</span><span class="lead">One-sentence takeaway.</span></div><p>Key points, decisions, numbers and action items, with <b>key terms</b> in bold.</p></aside>
   This document is built from the transcript alone, so do NOT compare it with anything: no NEW / CONFIRMS / UPDATES callouts, no "Prior view" lines, no remarks like "not in any prior document" - even when a previous version, earlier documents or a knowledge base are listed above (use those only for names, terms and context). If the style reference has no summary callout, add a neutral one in the same style (light and dark)."""

# Update: the new part is compared with the part already in the document and with prior sources.
CALLOUTS_COMPARE = """Callouts: after each meaningful topic of the NEW part, add <aside class="callout new|confirm|update"> blocks (🆕 NEW / ✅ CONFIRMS / 🔄 UPDATES) exactly as in the style reference, with a "Prior view:" line saying what we knew before and where it came from (the earlier part of this document, or a prior source). {callout_source}
   Keep the callouts already in the document (e.g. SUMMARY) as they are. If the file has no styles or legend entries for NEW / CONFIRMS / UPDATES yet, add them from the style reference."""

WRITE_RULE_FULL = """   Create it with the Write tool. For a long transcript, write the header plus the first sections first, then append the remaining sections in order with Edit (insert before the closing footer). Keep going until the file covers the whole transcript up to {t_last}."""

WRITE_RULE_UPDATE = """   The file ALREADY contains version {pv} of this document, covering the transcript up to {prev_t_last}. Update it IN PLACE with Edit; don't rewrite it from scratch:
   - Append everything said after {prev_t_last} (input lines {from_line}–{n_lines}) as new turns, sections, callouts and screenshot figures, before the footer.
   - Re-check the existing part only where the new material clarifies something (a speaker's identity, a name, a term, new screenshots), and fix those spots.
   - Update the header (time range, Who's speaking) and the footer (version, line count, time range)."""


def _rel(path: Path, start: Path) -> str:
    return os.path.relpath(path, start).replace(os.sep, "/")


def _fmt_shots(shots: list[dict], out_dir: Path) -> str:
    if not shots:
        return "  (no screenshots in this session yet)"
    lines = []
    for s in shots:
        p = Path(s["path"])
        ocr = p.with_suffix(".txt")
        lines.append(f"  - {s['time']}  {p}  → src=\"{_rel(p, out_dir)}\""
                     + (f"  (OCR: {ocr.name})" if ocr.exists() else ""))
    return "\n".join(lines)


def prepare(project: str, sid: str, mode: str = "update", live: bool = False) -> dict:
    """Snapshot the transcript, allocate the next version and build the prompt."""
    settings = load_settings()
    psettings = load_project_settings(project)
    src = transcript_path(project, sid)
    if not src.exists():
        raise FileNotFoundError(f"Transcript not found: {src}")

    sdir = session_dir(project, sid)
    sdir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(project, sid)
    prev = latest_version(manifest)
    if mode == "update" and (not prev or not (sdir / prev["html"]).exists()):
        mode = "full"

    v = max([x["v"] for x in manifest["versions"]] + [0]) + 1
    snapshot = sdir / f"input_v{v}.txt"
    # Without the lines/screenshots hidden in the UI, with the visible auto frames.
    text = beautify_text(project, sid)
    snapshot.write_text(text, encoding="utf-8")
    entries = parse_transcript(text)
    raw_lines = text.splitlines()
    timed = [l for l in raw_lines if l.startswith("[")]
    t_first = entries[0]["time"] if entries else "-"
    t_last = entries[-1]["time"] if entries else "-"
    shots = []
    for e in entries:
        if e["kind"] == "screenshot" and Path(e["path"]).exists():
            auto = Path(e["path"]).name.startswith("frame_")   # auto frame: its line has the on-screen time
            shots.append({"path": e["path"], "time": e["time"] if auto else (shot_time(e["path"]) or e["time"])})

    out = sdir / f"v{v}.html"
    style = Path(psettings["style_reference"]).expanduser() if psettings["style_reference"] else DEFAULT_STYLE
    if not style.exists():
        style = DEFAULT_STYLE

    prev_block = ""
    write_rule = WRITE_RULE_FULL.format(t_last=t_last)
    from_line = 1
    if mode == "update":
        shutil.copyfile(sdir / prev["html"], out)
        prev_t_last = prev.get("t_last", "")
        for i, l in enumerate(raw_lines, 1):
            m = re.match(r"^\[(\d{2}:\d{2}:\d{2})\]", l)
            if m and m.group(1) > prev_t_last:
                from_line = i
                break
        else:
            from_line = len(raw_lines)
        write_rule = WRITE_RULE_UPDATE.format(pv=prev["v"], prev_t_last=prev_t_last,
                                              from_line=from_line, n_lines=len(raw_lines))
    elif prev and (sdir / prev["html"]).exists():
        prev_block = (f"- Previous version (v{prev['v']}, {prev.get('mode')}): {sdir / prev['html']}\n"
                      "  Use it only for consistency (speaker names, section names, terms); don't compare "
                      "with it. The transcript is the source of truth.\n")

    context_dir = psettings["context_dir"].strip()
    if context_dir and mode != "update":
        context_block = (
            f"- Project knowledge base: {context_dir}\n"
            "  If the claude-context MCP server is available, search it (search_code with this path) for "
            "names, terms and background: earlier documents, transcripts, notes.\n")
    elif context_dir:
        context_block = (
            f"- Project knowledge base: {context_dir}\n"
            "  If the claude-context MCP server is available, search it (search_code with this path) to check "
            "what is said against what we already know: earlier documents, transcripts, notes.\n")
        callout_source = ("Back up every NEW / CONFIRMS / UPDATES classification with a search of the "
                          "project knowledge base. Never invent prior knowledge.")
    else:
        context_block = ""
        callout_source = ("No knowledge base is configured, so compare against the earlier part of this "
                          "document and earlier documents of this project (if any). Where there is no prior "
                          "source, use NEW for new facts and decisions. Never invent prior knowledge.")
    callout_rule = (CALLOUTS_COMPARE.format(callout_source=callout_source) if mode == "update"
                    else CALLOUTS_SUMMARY)

    notes = load_notes(project).strip()
    notes_block = (
        f"- The user's notes for this project ({notes_path(project)}). They are AUTHORITATIVE: they cover who we are, "
        "the client, participants and terms. Apply every correction (heard → meant) everywhere in the transcript, "
        "including other spellings and mishearings of the same words. A line '\"quote\" <-- Name' means Name said "
        "that quote: attribute that turn to Name, and use it to recognise Name's voice elsewhere:\n"
        + "".join(f"    {l}\n" for l in notes.splitlines()) if notes else "")

    ext_files = external.for_session(project, sid)
    ext_block = (
        "- External transcript(s) of the SAME meeting from another tool (e.g. Microsoft Teams):\n"
        + "".join(f"    {p}\n" for p in ext_files)
        + "  Read them in full and merge them with ours into one better document:\n"
        "  - Speaker names there are reliable (they come from meeting accounts): use them to attribute turns.\n"
        "  - Brand names, product names and technical terms there are often misrecognized; ours (plus the notes "
        "and screenshots) usually has them right, so prefer our wording for those.\n"
        "  - Its timestamps may be relative to the start of that recording; align the two by content, but keep "
        "OUR times in the document.\n"
        "  - Where ours is garbled or missed something that the external one has, recover it from there.\n"
        "  - Say in the footer that an external transcript was used for speaker attribution.\n") if ext_files else ""

    if mode == "update" and (ext_files or "<--" in notes):
        write_rule += ("\n   - Re-check speaker attribution in the EXISTING part too, against the external transcript and the "
                       "'quote <-- Name' notes, and fix wrong or generic speaker labels (header chips included).")

    docs_dir = project_dir(project) / "docs"
    docs = sorted(p.name for p in docs_dir.glob("*") if p.suffix in (".html", ".txt", ".md")) if docs_dir.exists() else []
    docs_block = (f"- Earlier documents for this project (may help with names and context): {docs_dir}\n"
                  + "".join(f"    {d}\n" for d in docs[-15:])) if docs else ""

    mode_title = {"final": "FINAL version", "full": "current version", "update": "updated version"}[mode]
    live_note = (" (the session is still being recorded, so the transcript ends abruptly; that's expected)"
                 if live and mode != "final" else "")
    if mode == "final":
        live_note += ". The recording has ENDED, so this is the definitive document: identify every speaker."

    prompt = PROMPT.format(
        mode_title=mode_title, input=snapshot, n_lines=len(timed), t_first=t_first, t_last=t_last,
        live_note=live_note, shots=_fmt_shots(shots, sdir), style=style, prev_block=prev_block,
        context_block=context_block, notes_block=notes_block, ext_block=ext_block, docs_block=docs_block,
        callout_rule=callout_rule, v=v, now=datetime.now().strftime("%Y-%m-%d %H:%M"),
        out=out, write_rule=write_rule,
    )

    record = {
        "v": v, "mode": mode, "status": "running", "created": datetime.now().isoformat(timespec="seconds"),
        "html": out.name, "input": snapshot.name, "lines": len(timed), "t_first": t_first, "t_last": t_last,
        "screenshots": [Path(s["path"]).name for s in shots],
        "engine": settings["beautify_engine"],
        "model": llm_api.config(settings)["model"] if settings["beautify_engine"] == "api" else settings["beautify_model"],
    }
    manifest["versions"].append(record)
    save_manifest(project, sid, manifest)

    cwd = Path(context_dir).expanduser() if context_dir and Path(context_dir).expanduser().is_dir() else DATA_ROOT
    return {
        "project": project, "sid": sid, "v": v, "mode": mode, "out": out, "input": snapshot,
        "prompt": prompt, "cwd": cwd, "style": style, "settings": settings,
        "add_dirs": sorted({str(project_dir(project).resolve()), str(ROOT)}),
        "labels": {str(snapshot): "Reading the transcript", snapshot.name: "Reading the transcript",
                   str(style): "Studying the style reference", style.name: "Studying the style reference",
                   **({str(sdir / prev["html"]): f"Reading previous version v{prev['v']}"} if prev else {}),
                   **{str(p): "Reading the external transcript" for p in ext_files}},
        "ext_files": ext_files, "shot_count": len(shots), "lines": len(timed), "t_last": t_last,
    }


def claude_command(spec: dict) -> list[str]:
    # Writes only into this session's folder. The transcript, notes, OCR text and external
    # transcripts are untrusted input (anyone in the meeting can say "edit server.py"), so
    # no acceptEdits: -p denies every edit that isn't allowed by this rule.
    tools = ["Read", "Glob", "Grep", "TodoWrite", "mcp__claude-context",
             claude_cli.edit_rule(Path(spec["out"]).parent)]
    return claude_cli.build_command(spec["settings"]["beautify_model"], tools, spec["add_dirs"])


def html_stats(html: str, shot_names: list[str]) -> dict:
    times = TIME_RE.findall(html)
    title = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    return {
        "title": re.sub(r"\s+", " ", title.group(1)).strip() if title else "",
        "turns": html.count('class="turn"'),
        "callouts": len(re.findall(r'class="callout\b', html)),
        "figures": html.count('class="shot"'),
        "referenced": [n for n in shot_names if n in html],
        "doc_t_first": min(times) if times else "",
        "doc_t_last": max(times) if times else "",
        "size": len(html.encode("utf-8")),
    }


def _slug(title: str) -> str:
    t = re.split(r"\s[-–-]\s|\(", title)[0] if title else ""
    s = re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")
    return "-".join(s.split("-")[:6]) or "session"


def finish(project: str, sid: str, v: int, status: str, **info) -> dict:
    """Record the outcome of a version; on a successful final, copy it to docs/."""
    manifest = load_manifest(project, sid)
    rec = next(x for x in manifest["versions"] if x["v"] == v)
    rec.update(info)
    rec["status"] = status
    rec["finished"] = datetime.now().isoformat(timespec="seconds")
    out = session_dir(project, sid) / rec["html"]
    if status == "done":
        if not out.exists() or out.stat().st_size < 500:
            rec["status"] = "failed"
            rec["error"] = "The model finished without writing the document."
        else:
            rec.update(html_stats(out.read_text(encoding="utf-8", errors="replace"), rec["screenshots"]))
            if rec["mode"] == "final":
                rec["docs_copy"] = str(_copy_to_docs(project, sid, out, rec, manifest))
    save_manifest(project, sid, manifest)
    return rec


def _copy_to_docs(project: str, sid: str, out: Path, rec: dict, manifest: dict) -> Path:
    docs = project_dir(project) / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    existing = [x.get("docs_copy") for x in manifest["versions"] if x.get("docs_copy")]
    if existing:
        target = Path(existing[-1])
    else:
        dt = sid_datetime(sid)
        date = dt.strftime("%Y-%m-%d") if dt else sid
        target = docs / f"{date}-{_slug(rec.get('title', ''))}-annotated-transcript.html"
    html = out.read_text(encoding="utf-8")
    # vN.html lives in sessions/<sid>/, docs/ is one level closer to screenshots/
    html = html.replace('src="../../screenshots/', 'src="../screenshots/')
    html = html.replace('src="../../frames/', 'src="../frames/')
    target.write_text(html, encoding="utf-8")
    return target


# ── API engine (no Claude Code): single-shot generation via Gemini/OpenAI ───

def run_api(spec: dict, on_progress=None) -> None:
    """Single-pass Beautify with an API model (llm_api); on_progress(chars) while it writes."""
    import llm_api
    settings = spec["settings"]
    text = spec["input"].read_text(encoding="utf-8", errors="replace")
    ocr_parts = []
    for name in load_manifest(spec["project"], spec["sid"])["versions"][-1]["screenshots"]:
        txt = project_dir(spec["project"]) / "screenshots" / Path(name).with_suffix(".txt")
        if txt.exists():
            ocr_parts.append(f"--- {name} ---\n{txt.read_text(encoding='utf-8', errors='replace')}")
    ext = "".join(f"--- {p.name} ---\n{p.read_text(encoding='utf-8', errors='replace')}\n"
                  for p in spec.get("ext_files", []))
    prompt = (spec["prompt"].split("## Requirements")[1]
              .replace("Use the exact relative src listed for each screenshot above.",
                       "Use src=\"../../screenshots/<file name>\".")
              .split("8. Output file:")[0])
    dt = sid_datetime(spec["sid"])
    prompt = ("Create a FULL annotated transcript as one self-contained HTML document.\n"
              + (f"Recording date: {dt:%Y-%m-%d} (times in the transcript are local clock times).\n" if dt else "")
              + "\n## Requirements"
              + prompt + "\nReply with the HTML document only, with no markdown fences.\n\n"
              f"## STYLE REFERENCE (structure/CSS only)\n{spec['style'].read_text(encoding='utf-8')}\n\n"
              f"## SCREENSHOT OCR\n" + ("\n\n".join(ocr_parts) or "(none)") + f"\n\n## RAW TRANSCRIPT\n{text}\n"
              + (f"\n## EXTERNAL TRANSCRIPT OF THE SAME MEETING (reliable speaker names, often wrong terms; "
                 f"use it to attribute speakers, keep our terms and times)\n{ext}" if ext else ""))
    written = [0]

    def delta(d):
        written[0] += len(d)
        if on_progress:
            on_progress(written[0])
    try:
        html = llm_api.stream(llm_api.config(settings), [{"role": "user", "content": prompt}], delta).strip()
    except Exception as e:
        raise RuntimeError(llm_api.explain(e)) from e
    if html.startswith("```"):
        html = html.split("\n", 1)[1]
    if html.endswith("```"):
        html = html.rsplit("```", 1)[0].rstrip()
    # Not fix_unrendered_templates(): it turns "${...}" in the text into running script, and
    # the text comes from the transcript. Documents are static HTML (/files serves no script).
    spec["out"].write_text(html, encoding="utf-8")


# ── Console entry point ──────────────────────────────────────────────────────

def main():
    import native_helpers
    native_helpers.utf8_stdio()    # Windows pipes: cp1251 can't print → or Cyrillic
    ap = argparse.ArgumentParser(description="Beautify a transcript into a full annotated HTML document")
    ap.add_argument("project")
    ap.add_argument("sid", help="Session id, e.g. 20260928_110051 (or 'latest')")
    ap.add_argument("--mode", choices=["update", "full", "final"], default="full")
    ap.add_argument("--model", default=None, help="Claude model alias (default from settings: opus)")
    args = ap.parse_args()

    sid = args.sid
    if sid == "latest":
        from sessions import list_sessions
        sid = list_sessions(args.project)[0]["sid"]
    spec = prepare(args.project, sid, args.mode)
    if args.model:
        spec["settings"]["beautify_model"] = args.model
    print(f"Beautify v{spec['v']} ({spec['mode']}) → {spec['out']}")
    started = time.time()

    if spec["settings"]["beautify_engine"] == "api":
        try:
            run_api(spec)
            rec = finish(args.project, sid, spec["v"], "done", duration=round(time.time() - started))
        except Exception as e:
            rec = finish(args.project, sid, spec["v"], "failed", error=str(e))
    else:
        proc = subprocess.Popen(claude_command(spec), cwd=spec["cwd"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        proc.stdin.write(spec["prompt"])
        proc.stdin.close()
        result = {}
        for line in proc.stdout:
            ev = claude_cli.parse_line(line)
            if not ev:
                continue
            for name, inp in claude_cli.tool_uses(ev):
                print(f"  [{time.time() - started:6.0f}s] {claude_cli.describe_tool(name, inp, spec['labels'])}")
            if ev.get("type") == "result":
                result = ev
        proc.wait()
        ok = proc.returncode == 0 and not result.get("is_error")
        rec = finish(args.project, sid, spec["v"], "done" if ok else "failed",
                     duration=round(time.time() - started), summary=(result.get("result") or "")[:500],
                     cost=result.get("total_cost_usd"))
    print(f"Status: {rec['status']}  {rec.get('summary', rec.get('error', ''))}")
    print(f"Output: {spec['out']}")
    if rec.get("docs_copy"):
        print(f"Docs copy: {rec['docs_copy']}")
    sys.exit(0 if rec["status"] == "done" else 1)


if __name__ == "__main__":
    main()
