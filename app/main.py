from __future__ import annotations

import html
import json
import shutil
import threading
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .processor import (
    ROOT,
    build_clip_suggestions_for_source,
    build_job_ranges_from_suggestions,
    delete_youtube_cookies,
    fetch_youtube_preview,
    ffmpeg_path,
    format_timecode,
    get_youtube_cookies_status,
    ingest_youtube_source,
    llm_status,
    parse_ranges,
    parse_timecode,
    run_job,
    save_youtube_cookies,
    search_transcript_for_source,
    yt_dlp_path,
)

UPLOADS_DIR = ROOT / "uploads"
OUTPUTS_DIR = ROOT / "outputs"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

APP_NAME = "Clipper"


app = FastAPI(title=APP_NAME)
app.mount("/downloads", StaticFiles(directory=str(OUTPUTS_DIR)), name="downloads")
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")

PREVIEW_TRANSCRIPT_LIMIT = 1500
SUGGESTION_LIMIT = 5


def launch_job(job_id: int) -> None:
    thread = threading.Thread(target=run_job, args=(job_id,), daemon=True)
    thread.start()


def format_duration_label(seconds: int | float | None) -> str:
    if not seconds:
        return "-"
    return format_timecode(float(seconds))


def _checkbox(checked: bool) -> str:
    return "checked" if checked else ""


def _status_badge(label: str, tone: str) -> str:
    safe = html.escape(label)
    return f"<span class='badge badge-{tone}'>{safe}</span>"


def _ai_status_badge(value: str) -> str:
    raw = (value or "-").strip()
    lowered = raw.lower()
    if lowered == "applied":
        return _status_badge(f"ai: {raw}", "success")
    if lowered.startswith("fallback"):
        return _status_badge("ai: fallback", "warn")
    if lowered in {"not_configured", "no_transcript", "not_requested"}:
        return _status_badge(f"ai: {raw}", "muted")
    return _status_badge(f"ai: {raw}", "info")


def _cache_status_badge(value: str, ttl_seconds: int | str | None) -> str:
    raw = (value or "miss").strip()
    lowered = raw.lower()
    label = f"cache: {raw}"
    if ttl_seconds not in (None, ""):
        label += f" ({ttl_seconds}s)"
    if lowered.startswith("hit"):
        return _status_badge(label, "success")
    if lowered == "miss":
        return _status_badge(label, "info")
    return _status_badge(label, "muted")


def render_home(
    preview: dict | None = None,
    error_message: str | None = None,
    suggestion_payload: dict | None = None,
    search_payload: dict | None = None,
) -> str:
    sources = db.list_sources()
    jobs = db.list_jobs_with_sources()
    cookies_status = get_youtube_cookies_status()
    llm = llm_status()

    source_options = "".join(
        f'<option value="{row["id"]}">#{row["id"]} — {html.escape(row["title"])}</option>' for row in sources
    )
    if not source_options:
        source_options = '<option value="">Belum ada source</option>'

    source_rows = []
    for row in sources:
        metadata = json.loads(row["metadata_json"] or "{}")
        source_name = Path(row["input_path"]).name
        duration_label = format_duration_label(metadata.get("duration"))
        source_mode = metadata.get("storage_mode") or ("local_file" if Path(row["input_path"]).exists() else "remote")
        meta_bits = []
        if metadata.get("channel"):
            meta_bits.append(f"channel: {html.escape(str(metadata['channel']))}")
        if metadata.get("webpage_url"):
            safe_url = html.escape(str(metadata["webpage_url"]))
            meta_bits.append(f"url: <a href='{safe_url}'>{safe_url}</a>")
        if metadata.get("subtitle_available"):
            meta_bits.append(f"subtitle: {html.escape(str(metadata.get('subtitle_lang') or 'yes'))}")
        if source_mode:
            meta_bits.append(f"mode: {html.escape(str(source_mode))}")
        transcript_links = []
        if metadata.get("transcript_path"):
            transcript_links.append(f"<a href='/sources/{row['id']}/transcript'>transcript</a>")
        if metadata.get("transcript_char_count"):
            transcript_links.append(f"{metadata['transcript_char_count']} chars")
        if metadata.get("transcript_cue_count"):
            transcript_links.append(f"{metadata['transcript_cue_count']} cues")
        if transcript_links:
            meta_bits.append(" | ".join(transcript_links))
        if metadata.get("transcript_error"):
            meta_bits.append(f"transcript_error: {html.escape(str(metadata['transcript_error']))}")
        meta_html = "<br>".join(meta_bits) if meta_bits else "-"
        file_html = html.escape(source_name)
        local_source_path = Path(row["input_path"])
        if local_source_path.exists() and str(local_source_path).startswith(str(UPLOADS_DIR)):
            file_html = f"<a href='/uploads/{source_name}'>{source_name}</a>"
        elif metadata.get("webpage_url"):
            remote_url = html.escape(str(metadata["webpage_url"]))
            file_html = f"<a href='{remote_url}'>remote-source</a>"
        source_rows.append(
            f"<tr><td>{row['id']}</td><td>{html.escape(row['title'])}</td><td>{row['kind']}</td><td>{duration_label}</td><td>{meta_html}</td><td>{file_html}</td></tr>"
        )
    source_rows_html = "".join(source_rows) or "<tr><td colspan='6'>Belum ada source</td></tr>"

    job_rows = []
    for job in jobs:
        clips = db.list_clips_for_job(job["id"])
        clip_links = []
        for clip in clips:
            clip_path = Path(clip["output_path"])
            clip_name = html.escape(clip_path.name)
            try:
                rel = clip_path.relative_to(OUTPUTS_DIR).as_posix()
            except ValueError:
                clip_links.append(clip_name)
                continue
            clip_links.append(f"<a href='/downloads/{rel}'>{clip_name}</a>")
        clip_links_html = "<br>".join(clip_links) or "-"
        error_block = f"<div style='color:#fca5a5;margin-top:6px'>{html.escape(job['error'])}</div>" if job["error"] else ""
        job_rows.append(
            f"<tr><td>{job['id']}</td><td>{html.escape(job['source_title'])}</td><td>{job['status']}</td><td><code>{html.escape(job['clip_ranges_json'])}</code>{error_block}</td><td>{clip_links_html}</td></tr>"
        )
    job_rows_html = "".join(job_rows) or "<tr><td colspan='5'>Belum ada job</td></tr>"

    preview_html = ""
    if preview:
        excerpt_raw = preview.get("transcript_excerpt", "")[:PREVIEW_TRANSCRIPT_LIMIT]
        transcript_error = html.escape(preview.get("transcript_error") or "")
        if excerpt_raw:
            excerpt = html.escape(excerpt_raw).replace("\n", "<br>")
        elif transcript_error:
            excerpt = f"<span class='muted'>Transcript belum bisa diambil: {transcript_error}</span>"
        else:
            excerpt = "<span class='muted'>Preview transcript kosong</span>"
        thumb_html = ""
        if preview.get("thumbnail"):
            thumb_html = f"<img src='{html.escape(preview['thumbnail'])}' alt='thumbnail' style='max-width:220px;border-radius:10px;margin-top:10px'>"
        preview_title = html.escape(preview.get("title") or preview.get("webpage_url") or "Preview")
        preview_url = html.escape(preview.get("webpage_url") or "")
        cookies_text = "aktif" if preview.get("cookies_enabled") else "off"
        preview_html = f"""
        <div class='card'>
          <h2>Preview YouTube</h2>
          <p><strong>{preview_title}</strong></p>
          <p class='muted'>channel: {html.escape(preview.get('channel') or '-')} · durasi: {format_duration_label(preview.get('duration'))} · subtitle: {html.escape(preview.get('subtitle_lang') or 'tidak ada')} · cookies: {cookies_text}</p>
          <p><a href='{preview_url}'>{preview_url}</a></p>
          {thumb_html}
          <h3 style='margin-top:16px'>Preview transcript</h3>
          <div style='background:#0f172a;padding:12px;border-radius:8px;line-height:1.55'>{excerpt}</div>
          <p class='muted' style='margin-top:10px'>chars: {preview.get('transcript_char_count', 0)} · cues: {preview.get('transcript_cue_count', 0)}</p>
        </div>
        """

    suggestion_html = ""
    if suggestion_payload:
        suggestion_rows = []
        for idx, item in enumerate(suggestion_payload.get("suggestions", [])):
            rank = item.get("rank") or (idx + 1)
            score = item.get("score")
            score_text = f"{float(score):.1f}" if score is not None else "-"
            suggestion_rows.append(
                f"""
                <tr>
                  <td><input type='checkbox' name='selected_{idx}' value='1' style='width:auto' checked></td>
                  <td>{rank}</td>
                  <td><input type='text' name='start_{idx}' value='{html.escape(format_timecode(float(item['start'])))}'></td>
                  <td><input type='text' name='end_{idx}' value='{html.escape(format_timecode(float(item['end'])))}'></td>
                  <td>{score_text}</td>
                  <td>
                    <div><strong>{html.escape(item.get('label') or '-')}</strong></div>
                    <div class='muted'>{html.escape(item.get('reason') or '-')}</div>
                    <input type='hidden' name='label_{idx}' value='{html.escape(item.get('label') or '')}'>
                  </td>
                </tr>
                """
            )
        rows_html = "".join(suggestion_rows) or "<tr><td colspan='6'>Belum ada suggestion</td></tr>"
        ai_status = str(suggestion_payload.get("ai_status") or "-")
        ai_note = ""
        hidden_use_ai = ""
        if suggestion_payload.get("use_ai"):
            provider = suggestion_payload.get("llm_provider") or "custom"
            provider_label = "Meow Labs" if provider == "meowlabs" else str(provider)
            model_label = html.escape(str(suggestion_payload.get("llm_model") or "-"))
            cache_label = str(suggestion_payload.get("cache_status") or "miss")
            cache_ttl = suggestion_payload.get("cache_ttl_seconds") or "-"
            badges = " ".join([
                _ai_status_badge(ai_status),
                _cache_status_badge(cache_label, cache_ttl),
                _status_badge(f"provider: {provider_label}", "info"),
                _status_badge(f"model: {suggestion_payload.get('llm_model') or '-'}", "muted"),
            ])
            ai_note = f" · {badges}"
            hidden_use_ai = "<input type='hidden' name='use_ai' value='1' />"
        suggestion_html = f"""
        <div class='card'>
          <h2>Suggest + Phase 6 Picker</h2>
          <p class='muted'>source: {html.escape(suggestion_payload.get('source_title') or '-')} · strategy: {html.escape(suggestion_payload.get('strategy') or '-')} · clip_length: {suggestion_payload.get('clip_length')}{ai_note}</p>
          <form action='/sources/custom-job' method='post'>
            <input type='hidden' name='source_id' value='{suggestion_payload.get("source_id")}' />
            <input type='hidden' name='clip_length' value='{suggestion_payload.get("clip_length")}' />
            {hidden_use_ai}
            <input type='hidden' name='total_suggestions' value='{len(suggestion_payload.get("suggestions", []))}' />
            <table>
              <thead><tr><th>Pilih</th><th>Rank</th><th>Start</th><th>End</th><th>Score</th><th>Label / Reason</th></tr></thead>
              <tbody>{rows_html}</tbody>
            </table>
            <button type='submit' style='margin-top:14px'>Generate dari pilihan + edit range di atas</button>
          </form>
        </div>
        """

    search_html = ""
    if search_payload:
        rows = []
        for item in search_payload.get("results", []):
            rows.append(
                f"<tr><td><code>{html.escape(item['range'])}</code></td><td>{html.escape(item.get('text') or '-')}</td></tr>"
            )
        rows_html = "".join(rows) or "<tr><td colspan='2'>Tidak ada hasil</td></tr>"
        search_html = f"""
        <div class='card'>
          <h2>Transcript Search</h2>
          <p class='muted'>source: {html.escape(search_payload.get('source_title') or '-')} · query: {html.escape(search_payload.get('query') or '')} · strategy: {html.escape(search_payload.get('strategy') or '-')}</p>
          <table>
            <thead><tr><th>Range</th><th>Text</th></tr></thead>
            <tbody>{rows_html}</tbody>
          </table>
        </div>
        """

    error_html = ""
    if error_message:
        error_html = f"<div class='card' style='border:1px solid #7f1d1d;color:#fecaca'>{html.escape(error_message)}</div>"

    cookies_summary = "aktif" if cookies_status["exists"] else "belum ada"
    llm_summary = "belum dikonfigurasi"
    if llm["ready"]:
        provider_label = "Meow Labs" if llm.get("provider") == "meowlabs" else (llm.get("provider") or "custom")
        llm_summary = f"aktif via {html.escape(provider_label)} ({html.escape(llm['model'])})"

    return f"""<!doctype html>
<html>
<head>
  <meta charset='utf-8'>
  <title>{APP_NAME}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; background: #0b1220; color: #e5e7eb; }}
    .card {{ background: #111827; padding: 20px; border-radius: 12px; margin-bottom: 18px; }}
    input, textarea, select, button {{ width: 100%; padding: 10px; margin-top: 8px; border-radius: 8px; border: 1px solid #374151; background: #0f172a; color: #e5e7eb; box-sizing: border-box; }}
    button {{ background: #2563eb; cursor: pointer; font-weight: 700; }}
    button.secondary {{ background: #1d4ed8; }}
    button.danger {{ background: #7f1d1d; }}
    table {{ width: 100%; border-collapse: collapse; }}
    td, th {{ border-bottom: 1px solid #1f2937; padding: 8px; text-align: left; vertical-align: top; }}
    a {{ color: #60a5fa; word-break: break-all; }}
    code {{ white-space: pre-wrap; }}
    .grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 18px; }}
    .muted {{ color: #94a3b8; }}
    .inline-check {{ display:flex; align-items:center; gap:8px; margin-top:10px; }}
    .inline-check input {{ width:auto; margin:0; }}
    .badge {{ display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; font-weight:700; margin-right:6px; margin-top:4px; }}
    .badge-success {{ background:#14532d; color:#bbf7d0; border:1px solid #166534; }}
    .badge-warn {{ background:#78350f; color:#fde68a; border:1px solid #92400e; }}
    .badge-info {{ background:#1e3a8a; color:#bfdbfe; border:1px solid #1d4ed8; }}
    .badge-muted {{ background:#374151; color:#e5e7eb; border:1px solid #4b5563; }}
  </style>
</head>
<body>
  <h1>{APP_NAME}</h1>
  <p class='muted'>Transcript-first video clipper buat potong source lokal atau YouTube dari satu panel web yang ringan.</p>
  {error_html}

  <div class='grid'>
    <div class='card'>
      <h2>Upload source video</h2>
      <form action='/sources/upload' method='post' enctype='multipart/form-data'>
        <label>Judul</label>
        <input name='title' placeholder='contoh: podcast-episode-12' required>
        <label>File video</label>
        <input type='file' name='video' accept='video/*' required>
        <button type='submit'>Upload source</button>
      </form>
    </div>

    <div class='card'>
      <h2>Ingest YouTube URL</h2>
      <form action='/sources/youtube' method='post'>
        <label>URL YouTube</label>
        <input name='url' placeholder='https://www.youtube.com/watch?v=...' required>
        <label>Override judul (opsional)</label>
        <input name='title' placeholder='biarkan kosong untuk pakai judul video'>
        <button type='submit'>Simpan transcript-first source</button>
      </form>
      <form action='/youtube/preview' method='post' style='margin-top:14px'>
        <label>Preview metadata + transcript</label>
        <input name='url' placeholder='https://www.youtube.com/watch?v=...' required>
        <button type='submit' class='secondary'>Preview dulu</button>
      </form>
    </div>

    <div class='card'>
      <h2>Buat clip job manual</h2>
      <form action='/jobs' method='post'>
        <label>Pilih source</label>
        <select name='source_id' required>{source_options}</select>
        <label>Range per baris</label>
        <textarea name='clip_ranges' rows='8' placeholder='00:00:02-00:00:06&#10;00:00:07-00:00:10' required></textarea>
        <button type='submit'>Proses clip</button>
      </form>
    </div>
  </div>

  <div class='grid'>
    <div class='card'>
      <h2>YouTube cookies</h2>
      <p class='muted'>Status: {cookies_summary} · file: {html.escape(cookies_status['name']) if cookies_status['exists'] else '-'} · size: {cookies_status['size']} bytes</p>
      <form action='/settings/youtube-cookies' method='post' enctype='multipart/form-data'>
        <label>Upload cookies.txt</label>
        <input type='file' name='cookies_file' accept='.txt,text/plain' required>
        <button type='submit'>Upload / replace cookies</button>
      </form>
      <form action='/settings/youtube-cookies/delete' method='post' style='margin-top:12px'>
        <button type='submit' class='danger'>Hapus cookies</button>
      </form>
    </div>

    <div class='card'>
      <h2>Auto Suggest</h2>
      <form action='/sources/suggest' method='post'>
        <label>Pilih source</label>
        <select name='source_id' required>{source_options}</select>
        <label>Panjang clip (detik)</label>
        <input type='number' name='clip_length' min='5' max='120' value='30' required>
        <label class='inline-check'><input type='checkbox' name='use_ai' value='1'> AI rerank suggestion</label>
        <p class='muted'>LLM: {llm_summary}</p>
        <button type='submit' class='secondary'>Generate suggestion</button>
      </form>
    </div>

    <div class='card'>
      <h2>Transcript Search</h2>
      <form action='/sources/search' method='post'>
        <label>Pilih source</label>
        <select name='source_id' required>{source_options}</select>
        <label>Keyword</label>
        <input name='query' placeholder='contoh: never gonna' required>
        <button type='submit' class='secondary'>Cari di transcript</button>
      </form>
    </div>
  </div>

  {preview_html}
  {suggestion_html}
  {search_html}

  <div class='card'>
    <h2>Sources</h2>
    <table>
      <thead><tr><th>ID</th><th>Title</th><th>Kind</th><th>Duration</th><th>Metadata</th><th>File</th></tr></thead>
      <tbody>{source_rows_html}</tbody>
    </table>
  </div>

  <div class='card'>
    <h2>Jobs</h2>
    <table>
      <thead><tr><th>ID</th><th>Source</th><th>Status</th><th>Ranges / Error</th><th>Output</th></tr></thead>
      <tbody>{job_rows_html}</tbody>
    </table>
  </div>
</body>
</html>"""


@app.on_event("startup")
def startup() -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()


@app.get("/health")
def health() -> dict:
    llm = llm_status()
    return {
        "ok": True,
        "ffmpeg_path": ffmpeg_path(),
        "yt_dlp_path": yt_dlp_path(),
        "sources": len(db.list_sources()),
        "jobs": len(db.list_jobs_with_sources()),
        "cookies_enabled": get_youtube_cookies_status()["exists"],
        "llm_ready": llm["ready"],
        "llm_provider": llm.get("provider"),
        "llm_model": llm["model"],
    }


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return render_home()


@app.post("/youtube/preview", response_class=HTMLResponse)
def youtube_preview(url: str = Form(...)) -> str:
    try:
        preview = fetch_youtube_preview(url.strip())
        return render_home(preview=preview)
    except Exception as exc:
        return render_home(error_message=f"Preview gagal: {exc}")


@app.get("/api/youtube/preview")
def youtube_preview_api(url: str):
    return JSONResponse(fetch_youtube_preview(url.strip()))


@app.post("/settings/youtube-cookies")
def upload_youtube_cookies(cookies_file: UploadFile = File(...)):
    raw = cookies_file.file.read().decode("utf-8", "ignore")
    save_youtube_cookies(raw)
    return RedirectResponse(url="/", status_code=303)


@app.post("/settings/youtube-cookies/delete")
def remove_youtube_cookies():
    delete_youtube_cookies()
    return RedirectResponse(url="/", status_code=303)


@app.post("/sources/upload")
def upload_source(title: str = Form(...), video: UploadFile = File(...)):
    suffix = Path(video.filename or "upload.mp4").suffix or ".mp4"
    dest = UPLOADS_DIR / f"{uuid4().hex}{suffix}"
    with dest.open("wb") as fh:
        shutil.copyfileobj(video.file, fh)
    db.insert_source(title=title.strip(), kind="upload", input_path=str(dest), metadata={"original_name": video.filename})
    return RedirectResponse(url="/", status_code=303)


@app.post("/sources/youtube")
def create_youtube_source(url: str = Form(...), title: str = Form("")):
    source_id = ingest_youtube_source(url=url.strip(), title=title.strip() or None)
    return RedirectResponse(url=f"/?source_id={source_id}", status_code=303)


@app.get("/sources/{source_id}/transcript")
def source_transcript(source_id: int):
    source = db.get_source(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="source tidak ditemukan")
    metadata = json.loads(source["metadata_json"] or "{}")
    transcript_rel = metadata.get("transcript_path")
    if not transcript_rel:
        raise HTTPException(status_code=404, detail="transcript tidak tersedia")
    transcript_path = ROOT / transcript_rel
    if not transcript_path.exists():
        raise HTTPException(status_code=404, detail="file transcript tidak ditemukan")
    return PlainTextResponse(transcript_path.read_text(errors="ignore"))


@app.post("/sources/suggest", response_class=HTMLResponse)
def source_suggest(source_id: int = Form(...), clip_length: float = Form(30.0), use_ai: str | None = Form(None)) -> str:
    try:
        payload = build_clip_suggestions_for_source(
            source_id=source_id,
            clip_length=clip_length,
            limit=SUGGESTION_LIMIT,
            use_ai=bool(use_ai),
        )
        return render_home(suggestion_payload=payload)
    except Exception as exc:
        return render_home(error_message=f"Suggestion gagal: {exc}")


@app.get("/api/sources/{source_id}/suggestions")
def source_suggestions_api(source_id: int, clip_length: float = 30.0, use_ai: bool = False):
    return JSONResponse(
        build_clip_suggestions_for_source(
            source_id=source_id,
            clip_length=clip_length,
            limit=SUGGESTION_LIMIT,
            use_ai=use_ai,
        )
    )


@app.post("/sources/suggest-to-job")
def source_suggest_to_job(source_id: int = Form(...), clip_length: float = Form(30.0), use_ai: str | None = Form(None)):
    source = db.get_source(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="source tidak ditemukan")
    payload = build_job_ranges_from_suggestions(
        source_id=source_id,
        clip_length=clip_length,
        limit=SUGGESTION_LIMIT,
        use_ai=bool(use_ai),
    )
    if not payload["clip_ranges"]:
        raise HTTPException(status_code=400, detail="suggestion kosong")
    job_id = db.insert_job(source_id=source_id, clip_ranges=payload["clip_ranges"])
    launch_job(job_id)
    return RedirectResponse(url=f"/?job_id={job_id}", status_code=303)


@app.get("/api/sources/{source_id}/suggestions-to-job")
def source_suggestions_to_job_api(source_id: int, clip_length: float = 30.0, use_ai: bool = False):
    source = db.get_source(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="source tidak ditemukan")
    payload = build_job_ranges_from_suggestions(
        source_id=source_id,
        clip_length=clip_length,
        limit=SUGGESTION_LIMIT,
        use_ai=use_ai,
    )
    if not payload["clip_ranges"]:
        raise HTTPException(status_code=400, detail="suggestion kosong")
    job_id = db.insert_job(source_id=source_id, clip_ranges=payload["clip_ranges"])
    launch_job(job_id)
    return JSONResponse({"job_id": job_id, **payload})


@app.post("/sources/custom-job")
async def source_custom_job(request: Request):
    form = await request.form()
    source_id = int(str(form.get("source_id") or "0"))
    clip_length = float(str(form.get("clip_length") or "30"))
    total = int(str(form.get("total_suggestions") or "0"))
    use_ai = bool(form.get("use_ai"))
    source = db.get_source(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="source tidak ditemukan")

    selected: list[dict] = []
    for idx in range(total):
        if not form.get(f"selected_{idx}"):
            continue
        start_raw = str(form.get(f"start_{idx}") or "").strip()
        end_raw = str(form.get(f"end_{idx}") or "").strip()
        label = str(form.get(f"label_{idx}") or f"suggested-{idx + 1}").strip()
        if not start_raw or not end_raw:
            continue
        start = parse_timecode(start_raw)
        end = parse_timecode(end_raw)
        if end <= start:
            raise HTTPException(status_code=400, detail=f"range suggestion #{idx + 1} tidak valid")
        selected.append({"start": start, "end": end, "label": label})

    if not selected:
        raise HTTPException(status_code=400, detail="pilih minimal 1 suggestion")

    job_id = db.insert_job(source_id=source_id, clip_ranges=selected)
    launch_job(job_id)
    return RedirectResponse(url=f"/?job_id={job_id}&clip_length={clip_length}&use_ai={1 if use_ai else 0}", status_code=303)


@app.post("/sources/search", response_class=HTMLResponse)
def source_search(source_id: int = Form(...), query: str = Form(...)) -> str:
    try:
        payload = search_transcript_for_source(source_id=source_id, query=query)
        return render_home(search_payload=payload)
    except Exception as exc:
        return render_home(error_message=f"Search transcript gagal: {exc}")


@app.get("/api/sources/{source_id}/transcript-search")
def source_search_api(source_id: int, query: str):
    return JSONResponse(search_transcript_for_source(source_id=source_id, query=query))


@app.post("/jobs")
def create_job(source_id: int = Form(...), clip_ranges: str = Form(...)):
    source = db.get_source(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="source tidak ditemukan")
    ranges = parse_ranges(clip_ranges)
    job_id = db.insert_job(source_id=source_id, clip_ranges=ranges)
    launch_job(job_id)
    return RedirectResponse(url=f"/?job_id={job_id}", status_code=303)


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: int):
    job = next((row for row in db.list_jobs_with_sources() if row["id"] == job_id), None)
    if not job:
        raise HTTPException(status_code=404, detail="job tidak ditemukan")
    clips = [dict(row) for row in db.list_clips_for_job(job_id)]
    return JSONResponse({"job": dict(job), "clips": clips})
