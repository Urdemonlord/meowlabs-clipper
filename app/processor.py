from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import imageio_ffmpeg

from . import db

ROOT = Path(__file__).resolve().parents[1]
OUTPUTS_DIR = ROOT / "outputs"
UPLOADS_DIR = ROOT / "uploads"
TRANSCRIPTS_DIR = ROOT / "transcripts"
COOKIES_DIR = ROOT / "cookies"
TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
COOKIES_DIR.mkdir(parents=True, exist_ok=True)

SUBTITLE_LANGS = ["id", "en"]
DEFAULT_SUGGESTION_LENGTH = 30.0
DEFAULT_SUGGESTION_LIMIT = 5
YOUTUBE_COOKIES_PATH = COOKIES_DIR / "youtube-cookies.txt"
SUGGESTION_CACHE_TTL_SECONDS = int(os.environ.get("CLIPPER_SUGGESTION_CACHE_TTL", "180"))
_SUGGESTION_CACHE: dict[str, dict] = {}
_SUGGESTION_CACHE_LOCK = threading.Lock()


def parse_timecode(value: str) -> float:
    text = value.strip().replace(",", ".")
    if not text:
        raise ValueError("timecode kosong")
    parts = text.split(":")
    if len(parts) == 1:
        return float(parts[0])
    if len(parts) == 2:
        minutes, seconds = parts
        return int(minutes) * 60 + float(seconds)
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    raise ValueError(f"format timecode tidak valid: {value}")


def format_timecode(seconds: float) -> str:
    whole = max(0, int(seconds))
    hours, rem = divmod(whole, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def parse_ranges(raw: str) -> list[dict]:
    items: list[dict] = []
    for idx, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        if "-" not in line:
            raise ValueError(f"range baris {idx} harus format start-end")
        start_raw, end_raw = [part.strip() for part in line.split("-", 1)]
        start = parse_timecode(start_raw)
        end = parse_timecode(end_raw)
        if end <= start:
            raise ValueError(f"range baris {idx} punya end <= start")
        items.append({"start": start, "end": end, "label": f"clip-{idx}"})
    if not items:
        raise ValueError("minimal 1 range clip diperlukan")
    return items


def ffmpeg_path() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def yt_dlp_path() -> str:
    path = shutil.which("yt-dlp")
    if not path:
        raise RuntimeError("yt-dlp tidak ditemukan di PATH")
    return path


def _run_command(cmd: list[str], error_message: str) -> subprocess.CompletedProcess:
    completed = subprocess.run(cmd, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr[-2000:] or completed.stdout[-2000:] or error_message)
    return completed


def _normalize_language(value: str) -> str:
    return value.replace("_", "-").lower()


def _rank_subtitle_lang(lang: str) -> tuple[int, str]:
    normalized = _normalize_language(lang)
    for idx, preferred in enumerate(SUBTITLE_LANGS):
        if normalized == preferred or normalized.startswith(preferred + "-"):
            return (idx, normalized)
    return (len(SUBTITLE_LANGS) + 1, normalized)


def _choose_subtitle_language(data: dict) -> str | None:
    pools = [data.get("subtitles") or {}, data.get("automatic_captions") or {}]
    candidates: set[str] = set()
    for pool in pools:
        for lang, entries in pool.items():
            if entries:
                candidates.add(lang)
    if not candidates:
        return None
    return sorted(candidates, key=_rank_subtitle_lang)[0]


def parse_vtt_cues(content: str) -> list[dict]:
    cues: list[dict] = []
    blocks = re.split(r"\n\s*\n", content.replace("\r\n", "\n"))
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if lines[0] == "WEBVTT" or lines[0].startswith(("NOTE", "Kind:", "Language:")):
            continue
        if "-->" not in block:
            continue
        if "-->" not in lines[0]:
            if len(lines) < 2 or "-->" not in lines[1]:
                continue
            timing_line = lines[1]
            text_lines = lines[2:]
        else:
            timing_line = lines[0]
            text_lines = lines[1:]
        start_raw, end_raw = [part.strip().split(" ")[0] for part in timing_line.split("-->", 1)]
        text = " ".join(text_lines)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"&[a-z]+;", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        cues.append(
            {
                "start": parse_timecode(start_raw),
                "end": parse_timecode(end_raw),
                "text": text,
            }
        )
    deduped: list[dict] = []
    prev_text = None
    for cue in cues:
        if cue["text"] == prev_text:
            continue
        deduped.append(cue)
        prev_text = cue["text"]
    return deduped


def _plain_text_from_cues(cues: list[dict]) -> str:
    seen = set()
    lines: list[str] = []
    for cue in cues:
        text = cue["text"].strip()
        if not text or text in seen:
            continue
        seen.add(text)
        lines.append(text)
    return "\n".join(lines)


def get_youtube_cookies_status() -> dict:
    exists = YOUTUBE_COOKIES_PATH.exists()
    return {
        "exists": exists,
        "path": str(YOUTUBE_COOKIES_PATH),
        "name": YOUTUBE_COOKIES_PATH.name,
        "size": YOUTUBE_COOKIES_PATH.stat().st_size if exists else 0,
    }


def save_youtube_cookies(raw_text: str) -> dict:
    text = raw_text.replace("\r\n", "\n").strip()
    if not text:
        raise RuntimeError("cookies.txt kosong")
    if "youtube.com" not in text and ".youtube" not in text:
        raise RuntimeError("cookies.txt tidak terlihat seperti export YouTube")
    YOUTUBE_COOKIES_PATH.write_text(text + "\n")
    invalidate_suggestion_cache()
    return get_youtube_cookies_status()


def delete_youtube_cookies() -> None:
    if YOUTUBE_COOKIES_PATH.exists():
        YOUTUBE_COOKIES_PATH.unlink()
    invalidate_suggestion_cache()


def _append_ytdlp_cookies(cmd: list[str]) -> list[str]:
    cookies = get_youtube_cookies_status()
    if cookies["exists"]:
        return [*cmd, "--cookies", cookies["path"]]
    return cmd


def _build_ytdlp_cmd(*args: str) -> list[str]:
    return _append_ytdlp_cookies([yt_dlp_path(), *args])


def fetch_youtube_metadata(url: str) -> dict:
    cmd = _build_ytdlp_cmd(
        "--no-warnings",
        "--no-playlist",
        "--dump-single-json",
        url,
    )
    completed = _run_command(cmd, "gagal ambil metadata YouTube")
    data = json.loads(completed.stdout)
    subtitle_lang = _choose_subtitle_language(data)
    return {
        "title": data.get("title") or "",
        "webpage_url": data.get("webpage_url") or url,
        "channel": data.get("channel") or data.get("uploader") or "",
        "duration": data.get("duration") or 0,
        "thumbnail": data.get("thumbnail") or "",
        "extractor": data.get("extractor_key") or data.get("extractor") or "youtube",
        "original_id": data.get("id") or "",
        "description": data.get("description") or "",
        "subtitle_lang": subtitle_lang or "",
        "subtitle_available": bool(subtitle_lang),
        "cookies_enabled": get_youtube_cookies_status()["exists"],
    }


def _download_subtitle_payload(url: str, preferred_lang: str | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="clipper-sub-") as tmpdir:
        base = Path(tmpdir) / "subtitle"
        sub_langs = preferred_lang or ",".join(SUBTITLE_LANGS)
        cmd = _build_ytdlp_cmd(
            "--no-warnings",
            "--no-playlist",
            "--skip-download",
            "--write-auto-sub",
            "--write-sub",
            "--sub-format",
            "vtt",
            "--sub-langs",
            sub_langs,
            "-o",
            str(base) + ".%(ext)s",
            url,
        )
        completed = subprocess.run(cmd, capture_output=True, text=True)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr[-2000:] or completed.stdout[-2000:] or "gagal download subtitle")

        candidates = sorted(Path(tmpdir).glob("subtitle*.vtt"))
        if not candidates:
            raise RuntimeError("subtitle tidak tersedia untuk video ini")
        chosen = sorted(candidates, key=lambda p: _rank_subtitle_lang(p.stem.split('.')[-1]))[0]
        raw = chosen.read_text(errors="ignore")
        cues = parse_vtt_cues(raw)
        text = _plain_text_from_cues(cues)
        if not text:
            raise RuntimeError("subtitle berhasil diambil tapi kosong")
        return {"text": text, "cues": cues}


def fetch_youtube_preview(url: str) -> dict:
    metadata = fetch_youtube_metadata(url)
    transcript = ""
    transcript_error = ""
    transcript_cues: list[dict] = []
    if metadata.get("subtitle_available"):
        try:
            payload = _download_subtitle_payload(url, preferred_lang=metadata.get("subtitle_lang") or None)
            transcript = payload["text"]
            transcript_cues = payload["cues"]
        except Exception as exc:
            transcript_error = str(exc)
    preview = {
        **metadata,
        "transcript_excerpt": transcript[:1200],
        "transcript_char_count": len(transcript),
        "transcript_error": transcript_error,
        "transcript_cue_count": len(transcript_cues),
    }
    return preview


def _remote_youtube_metadata_base(url: str, metadata: dict) -> dict:
    remote_metadata = dict(metadata)
    remote_metadata["downloaded_from"] = url
    remote_metadata["storage_mode"] = "remote_transcript_first"
    return remote_metadata


def ingest_youtube_source(url: str, title: str | None = None) -> int:
    metadata = fetch_youtube_metadata(url)
    source_title = (title or "").strip() or metadata.get("title") or metadata.get("webpage_url") or url
    metadata = _remote_youtube_metadata_base(url, metadata)

    transcript_error = ""
    transcript_text = ""
    transcript_rel = ""
    transcript_segments_rel = ""
    if metadata.get("subtitle_available"):
        try:
            payload = _download_subtitle_payload(url, preferred_lang=metadata.get("subtitle_lang") or None)
            transcript_text = payload["text"]
            transcript_path = TRANSCRIPTS_DIR / f"yt-{uuid4().hex}.txt"
            transcript_path.write_text(transcript_text)
            transcript_rel = str(transcript_path.relative_to(ROOT))

            segments_path = TRANSCRIPTS_DIR / f"{transcript_path.stem}.segments.json"
            segments_path.write_text(json.dumps(payload["cues"], ensure_ascii=False, indent=2))
            transcript_segments_rel = str(segments_path.relative_to(ROOT))
        except Exception as exc:
            transcript_error = str(exc)
    metadata["transcript_path"] = transcript_rel
    metadata["transcript_segments_path"] = transcript_segments_rel
    metadata["transcript_excerpt"] = transcript_text[:1200]
    metadata["transcript_char_count"] = len(transcript_text)
    metadata["transcript_cue_count"] = 0
    if transcript_segments_rel:
        metadata["transcript_cue_count"] = len(json.loads((ROOT / transcript_segments_rel).read_text()))
    if transcript_error:
        metadata["transcript_error"] = transcript_error

    return db.insert_source(title=source_title, kind="youtube", input_path=url, metadata=metadata)


def _download_youtube_section(url: str, start_seconds: float, end_seconds: float) -> Path:
    if end_seconds <= start_seconds:
        raise RuntimeError("range clip YouTube tidak valid")
    temp_dir = Path(tempfile.mkdtemp(prefix="clipper-yt-section-"))
    output_template = temp_dir / "section.%(ext)s"
    section_spec = f"*{format_timecode(start_seconds)}-{format_timecode(end_seconds)}"
    cmd = _build_ytdlp_cmd(
        "--no-warnings",
        "--no-playlist",
        "-f",
        "mp4[height<=480]/bv*[height<=480]+ba/b[height<=480]/b",
        "--merge-output-format",
        "mp4",
        "--force-keyframes-at-cuts",
        "--ffmpeg-location",
        "/usr/bin",
        "--download-sections",
        section_spec,
        "-o",
        str(output_template),
        url,
    )
    try:
        _run_command(cmd, "gagal download section YouTube")
        matches = sorted(temp_dir.glob("section.*"))
        media_matches = [path for path in matches if path.is_file() and path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}]
        if not media_matches:
            raise RuntimeError("section YouTube selesai tapi file output tidak ditemukan")
        return media_matches[0]
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _resolve_job_input(job: dict, clip: dict) -> tuple[str, float, float, callable | None]:
    source_path = str(job["source_path"])
    source_file = Path(source_path)
    if source_file.exists():
        return (source_path, float(clip["start"]), float(clip["end"]), None)

    source = db.get_source(int(job["source_id"]))
    metadata = json.loads(source["metadata_json"] or "{}") if source else {}
    remote_url = metadata.get("downloaded_from") or metadata.get("webpage_url") or source_path
    if not remote_url or not str(remote_url).startswith(("http://", "https://")):
        raise RuntimeError("source file tidak ada dan URL remote tidak valid")

    start_seconds = float(clip["start"])
    end_seconds = float(clip["end"])
    section_path = _download_youtube_section(str(remote_url), start_seconds, end_seconds)
    return (str(section_path), 0.0, round(end_seconds - start_seconds, 3), lambda: shutil.rmtree(section_path.parent, ignore_errors=True))


def _load_transcript_cues_from_metadata(metadata: dict) -> list[dict]:
    rel = metadata.get("transcript_segments_path")
    if not rel:
        return []
    path = ROOT / rel
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    cues: list[dict] = []
    for item in data:
        try:
            cues.append(
                {
                    "start": float(item["start"]),
                    "end": float(item["end"]),
                    "text": str(item["text"]),
                }
            )
        except Exception:
            continue
    return cues


def _load_transcript_text_from_metadata(metadata: dict) -> str:
    rel = metadata.get("transcript_path")
    if rel:
        path = ROOT / rel
        if path.exists():
            return path.read_text(errors="ignore")
    cues = _load_transcript_cues_from_metadata(metadata)
    return _plain_text_from_cues(cues)


def _clip_label_from_text(text: str, fallback: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return fallback
    return cleaned[:120]


def _heuristic_text_score(text: str) -> float:
    words = [word for word in re.findall(r"[A-Za-zÀ-ÿ0-9']+", text.lower()) if word]
    unique_ratio = len(set(words)) / max(1, len(words))
    punctuation_bonus = 0.0
    if "?" in text:
        punctuation_bonus += 8.0
    if "!" in text:
        punctuation_bonus += 5.0
    keyword_bonus = 0.0
    for token in ["why", "how", "secret", "story", "mistake", "problem", "tips", "cara", "kenapa", "rahasia", "masalah"]:
        if token in text.lower():
            keyword_bonus += 3.0
    return round(min(100.0, 45.0 + len(words) * 1.4 + unique_ratio * 20.0 + punctuation_bonus + keyword_bonus), 2)


def _make_duration_fallback_suggestions(duration: float, clip_length: float = DEFAULT_SUGGESTION_LENGTH, limit: int = DEFAULT_SUGGESTION_LIMIT) -> list[dict]:
    if duration <= 0:
        return []
    actual_length = min(clip_length, duration)
    if duration <= actual_length:
        starts = [0.0]
    else:
        if limit <= 1:
            starts = [0.0]
        else:
            span = duration - actual_length
            starts = [round((span * idx) / (limit - 1), 3) for idx in range(limit)]
    out: list[dict] = []
    for idx, start in enumerate(starts, start=1):
        end = min(duration, start + actual_length)
        out.append(
            {
                "id": f"duration-{idx}",
                "start": round(start, 3),
                "end": round(end, 3),
                "range": f"{format_timecode(start)}-{format_timecode(end)}",
                "reason": f"fallback-duration-window-{idx}",
                "label": f"Clip suggestion {idx}",
                "score": round(max(40.0, 68.0 - idx * 3.0), 2),
            }
        )
    return out


def _build_transcript_candidates(cues: list[dict], duration: float, clip_length: float, limit: int) -> list[dict]:
    if not cues:
        return []
    raw_limit = max(limit * 2, limit)
    step = max(1, len(cues) // max(1, raw_limit))
    picked = cues[::step][:raw_limit]
    suggestions: list[dict] = []
    seen_ranges: set[str] = set()
    for idx, cue in enumerate(picked, start=1):
        start = max(0.0, cue["start"] - min(4.0, cue["start"]))
        end = cue["end"] + clip_length
        if duration > 0:
            end = min(duration, end)
        if end <= start:
            end = start + min(clip_length, duration or clip_length)
        range_text = f"{format_timecode(start)}-{format_timecode(end)}"
        if range_text in seen_ranges:
            continue
        seen_ranges.add(range_text)
        label = _clip_label_from_text(cue["text"], f"Cue {idx}")
        suggestions.append(
            {
                "id": f"cue-{idx}",
                "start": round(start, 3),
                "end": round(end, 3),
                "range": range_text,
                "reason": "transcript-match",
                "label": label,
                "score": _heuristic_text_score(cue["text"]),
                "source_text": cue["text"],
            }
        )
    suggestions.sort(key=lambda item: (-float(item.get("score") or 0), float(item["start"])))
    return suggestions[:raw_limit]


def _llm_settings() -> dict:
    meowlabs_key = os.environ.get("MEOWLABS_API_KEY") or ""
    api_key = os.environ.get("CLIPPER_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or meowlabs_key
    base_url = os.environ.get("CLIPPER_LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    model = os.environ.get("CLIPPER_LLM_MODEL") or os.environ.get("OPENAI_MODEL")

    if meowlabs_key and not base_url:
        base_url = "https://api.meowlabs.store/v1"
    if meowlabs_key and not model:
        model = "kr/glm-5"

    ready = bool(api_key and base_url and model)
    return {
        "ready": ready,
        "api_key": api_key or "",
        "base_url": base_url.rstrip("/") if base_url else "",
        "model": model or "",
    }


def llm_status() -> dict:
    settings = _llm_settings()
    provider = "custom"
    base_url = settings["base_url"].lower()
    if "api.meowlabs.store" in base_url:
        provider = "meowlabs"
    return {
        "ready": settings["ready"],
        "base_url": settings["base_url"],
        "model": settings["model"],
        "provider": provider,
    }


def _extract_json_block(text: str) -> dict:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    if fenced:
        return json.loads(fenced.group(1))
    plain = re.search(r"(\{.*\})", text, flags=re.S)
    if plain:
        return json.loads(plain.group(1))
    raise RuntimeError("respons AI tidak mengandung JSON")


def _chat_completion(messages: list[dict], max_tokens: int = 700) -> str:
    settings = _llm_settings()
    if not settings["ready"]:
        raise RuntimeError("AI suggest belum dikonfigurasi (set CLIPPER_LLM_API_KEY, CLIPPER_LLM_BASE_URL, CLIPPER_LLM_MODEL)")
    url = settings["base_url"]
    if not url.endswith("/chat/completions"):
        url = url + "/chat/completions"
    payload = json.dumps(
        {
            "model": settings["model"],
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings['api_key']}",
        },
    )

    last_error: Exception | None = None
    max_attempts = 3
    timeout_seconds = 20
    raw_body = ""
    content_type = ""
    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                raw_body = resp.read().decode("utf-8", "ignore")
                content_type = (resp.headers.get("content-type") or "").lower()
            break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "ignore")[:500]
            is_retryable = exc.code >= 500 or exc.code in {408, 409, 429}
            last_error = RuntimeError(f"AI suggest gagal ({exc.code}): {body or exc.reason}")
            if not is_retryable or attempt >= max_attempts:
                raise last_error from exc
            time.sleep(1.2 * attempt)
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            reason = getattr(exc, "reason", exc)
            last_error = RuntimeError(f"AI suggest timeout/gateway error (attempt {attempt}/{max_attempts}): {reason}")
            if attempt >= max_attempts:
                raise last_error from exc
            time.sleep(1.2 * attempt)
    else:
        raise last_error or RuntimeError("AI suggest gagal tanpa detail")

    if "text/event-stream" in content_type or raw_body.lstrip().startswith("data:"):
        text_chunks: list[str] = []
        for line in raw_body.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data_line = line[5:].strip()
            if not data_line or data_line == "[DONE]":
                continue
            try:
                event = json.loads(data_line)
            except json.JSONDecodeError:
                continue
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if isinstance(content, str):
                    text_chunks.append(content)
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            text_chunks.append(item.get("text") or "")
        content = "".join(text_chunks).strip()
        if not content:
            raise RuntimeError("AI suggest gagal: SSE content kosong")
        return content

    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"AI suggest gagal: response bukan JSON valid ({raw_body[:200]})") from exc

    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("AI suggest gagal: choices kosong")
    content = choices[0].get("message", {}).get("content")
    if isinstance(content, list):
        text_bits = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_bits.append(item.get("text") or "")
        content = "\n".join(text_bits)
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("AI suggest gagal: content kosong")
    return content


def _ai_rerank_suggestions(source_title: str, transcript_text: str, candidates: list[dict], clip_length: float, limit: int) -> list[dict]:
    clipped_transcript = transcript_text[:5000]
    candidate_lines = []
    for item in candidates:
        candidate_lines.append(
            f"- id={item['id']} range={item['range']} score={item.get('score', 0)} label={item.get('label', '')}"
        )
    prompt = (
        "Pilih clip paling menarik untuk short-form dari transcript. "
        "Prioritaskan hook, konflik, opini tajam, momentum, atau kalimat yang terdengar shareable. "
        "Balas JSON murni dengan format {\"picks\":[{\"id\":\"...\",\"score\":0-100,\"reason\":\"...\"}]} tanpa teks lain. "
        f"Maksimal {limit} picks.\n\n"
        f"Judul source: {source_title}\n"
        f"Target durasi clip: {clip_length} detik\n\n"
        f"Transcript excerpt:\n{clipped_transcript}\n\n"
        f"Candidates:\n" + "\n".join(candidate_lines)
    )
    content = _chat_completion(
        [
            {"role": "system", "content": "Kamu editor short-video yang ketat, ringkas, dan output JSON saja."},
            {"role": "user", "content": prompt},
        ]
    )
    parsed = _extract_json_block(content)
    picks = parsed.get("picks") or []
    by_id = {item["id"]: item for item in candidates}
    chosen: list[dict] = []
    seen: set[str] = set()
    for rank, pick in enumerate(picks, start=1):
        candidate_id = str(pick.get("id") or "").strip()
        if not candidate_id or candidate_id in seen or candidate_id not in by_id:
            continue
        seen.add(candidate_id)
        base = dict(by_id[candidate_id])
        base["score"] = float(pick.get("score") or base.get("score") or 0)
        base["reason"] = str(pick.get("reason") or "ai-rerank")[:240]
        base["rank"] = rank
        chosen.append(base)
        if len(chosen) >= limit:
            break
    if not chosen:
        raise RuntimeError("AI suggest mengembalikan picks kosong")
    return chosen


def _suggestion_cache_key(source: dict, clip_length: float, limit: int, use_ai: bool) -> str:
    llm = llm_status()
    payload = {
        "source_id": int(source["id"]),
        "title": source["title"],
        "metadata": source["metadata_json"] or "",
        "clip_length": round(float(clip_length), 3),
        "limit": int(limit),
        "use_ai": bool(use_ai),
        "llm_provider": llm.get("provider") if use_ai else "",
        "llm_model": llm.get("model") if use_ai else "",
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _get_cached_suggestions(cache_key: str) -> dict | None:
    now_mono = time.monotonic()
    now_epoch = time.time()
    with _SUGGESTION_CACHE_LOCK:
        cached = _SUGGESTION_CACHE.get(cache_key)
        if cached:
            if now_mono - float(cached.get("stored_at", 0)) > SUGGESTION_CACHE_TTL_SECONDS:
                _SUGGESTION_CACHE.pop(cache_key, None)
            else:
                payload = dict(cached["payload"])
                payload["cache_status"] = "hit-memory"
                payload["cache_ttl_seconds"] = SUGGESTION_CACHE_TTL_SECONDS
                return payload

    row = db.get_suggestion_cache(cache_key)
    if not row:
        return None
    created_at = float(row["created_at"])
    if now_epoch - created_at > SUGGESTION_CACHE_TTL_SECONDS:
        db.delete_suggestion_cache(cache_key=cache_key)
        return None
    payload = json.loads(row["payload_json"])
    payload["cache_status"] = "hit-sqlite"
    payload["cache_ttl_seconds"] = SUGGESTION_CACHE_TTL_SECONDS
    with _SUGGESTION_CACHE_LOCK:
        _SUGGESTION_CACHE[cache_key] = {
            "stored_at": now_mono,
            "payload": dict(payload),
        }
    return payload


def _store_cached_suggestions(cache_key: str, payload: dict) -> None:
    cached_payload = dict(payload)
    cached_payload["cache_status"] = "miss"
    cached_payload["cache_ttl_seconds"] = SUGGESTION_CACHE_TTL_SECONDS
    stored_at_mono = time.monotonic()
    with _SUGGESTION_CACHE_LOCK:
        _SUGGESTION_CACHE[cache_key] = {
            "stored_at": stored_at_mono,
            "payload": cached_payload,
        }
    db.upsert_suggestion_cache(
        cache_key=cache_key,
        source_id=int(payload.get("source_id")) if payload.get("source_id") is not None else None,
        payload=cached_payload,
        created_at=time.time(),
    )


def invalidate_suggestion_cache(source_id: int | None = None) -> None:
    with _SUGGESTION_CACHE_LOCK:
        if source_id is None:
            _SUGGESTION_CACHE.clear()
        else:
            doomed = [key for key, item in _SUGGESTION_CACHE.items() if int((item.get("payload") or {}).get("source_id") or -1) == source_id]
            for key in doomed:
                _SUGGESTION_CACHE.pop(key, None)
    db.delete_suggestion_cache(source_id=source_id) if source_id is not None else db.delete_suggestion_cache()


def build_clip_suggestions_for_source(
    source_id: int,
    clip_length: float = DEFAULT_SUGGESTION_LENGTH,
    limit: int = DEFAULT_SUGGESTION_LIMIT,
    use_ai: bool = False,
) -> dict:
    source = db.get_source(source_id)
    if not source:
        raise RuntimeError("source tidak ditemukan")

    cache_key = _suggestion_cache_key(source, clip_length=clip_length, limit=limit, use_ai=use_ai)
    cached_payload = _get_cached_suggestions(cache_key)
    if cached_payload:
        return cached_payload

    metadata = json.loads(source["metadata_json"] or "{}")
    duration = float(metadata.get("duration") or 0)
    cues = _load_transcript_cues_from_metadata(metadata)
    transcript_text = _load_transcript_text_from_metadata(metadata)

    strategy = "duration_fallback"
    ai_status = "not_requested"
    suggestions = _make_duration_fallback_suggestions(duration=duration, clip_length=clip_length, limit=limit)
    if cues:
        strategy = "transcript_cues"
        suggestions = _build_transcript_candidates(cues=cues, duration=duration, clip_length=clip_length, limit=limit)

    if use_ai:
        if not transcript_text.strip() or not suggestions:
            ai_status = "no_transcript"
        elif not llm_status()["ready"]:
            ai_status = "not_configured"
        else:
            try:
                ai_limit = min(limit, len(suggestions))
                suggestions = _ai_rerank_suggestions(
                    source_title=source["title"],
                    transcript_text=transcript_text,
                    candidates=suggestions,
                    clip_length=clip_length,
                    limit=ai_limit,
                )
                strategy = "ai_rerank"
                ai_status = "applied"
            except Exception as exc:
                ai_status = f"fallback: {exc}"

    if not suggestions and duration > 0:
        strategy = "duration_fallback"
        suggestions = _make_duration_fallback_suggestions(duration=duration, clip_length=clip_length, limit=limit)

    normalized: list[dict] = []
    for rank, item in enumerate(suggestions[:limit], start=1):
        normalized.append(
            {
                **item,
                "rank": rank,
                "range": item.get("range") or f"{format_timecode(float(item['start']))}-{format_timecode(float(item['end']))}",
                "score": round(float(item.get("score") or 0), 2),
            }
        )

    llm_meta = llm_status()
    payload = {
        "source_id": source_id,
        "source_title": source["title"],
        "strategy": strategy,
        "clip_length": clip_length,
        "use_ai": use_ai,
        "ai_status": ai_status,
        "llm_ready": llm_meta["ready"],
        "llm_provider": llm_meta["provider"],
        "llm_model": llm_meta["model"],
        "cache_status": "miss",
        "cache_ttl_seconds": SUGGESTION_CACHE_TTL_SECONDS,
        "suggestions": normalized,
    }
    _store_cached_suggestions(cache_key, payload)
    return payload


def build_job_ranges_from_suggestions(
    source_id: int,
    clip_length: float = DEFAULT_SUGGESTION_LENGTH,
    limit: int = DEFAULT_SUGGESTION_LIMIT,
    use_ai: bool = False,
    selected_suggestions: list[dict] | None = None,
) -> dict:
    payload = build_clip_suggestions_for_source(
        source_id=source_id,
        clip_length=clip_length,
        limit=limit,
        use_ai=use_ai,
    )
    source_suggestions = selected_suggestions if selected_suggestions is not None else payload["suggestions"]
    clip_ranges = []
    for idx, item in enumerate(source_suggestions, start=1):
        clip_ranges.append(
            {
                "start": float(item["start"]),
                "end": float(item["end"]),
                "label": str(item.get("label") or f"suggested-{idx}"),
            }
        )
    return {
        "source_id": payload["source_id"],
        "source_title": payload["source_title"],
        "strategy": payload["strategy"],
        "clip_length": payload["clip_length"],
        "use_ai": payload["use_ai"],
        "ai_status": payload["ai_status"],
        "clip_ranges": clip_ranges,
        "suggestions": payload["suggestions"],
    }


def search_transcript_for_source(source_id: int, query: str, limit: int = 10) -> dict:
    source = db.get_source(source_id)
    if not source:
        raise RuntimeError("source tidak ditemukan")
    metadata = json.loads(source["metadata_json"] or "{}")
    cues = _load_transcript_cues_from_metadata(metadata)
    needle = query.strip().lower()
    if not needle:
        return {"source_id": source_id, "source_title": source["title"], "query": query, "results": [], "strategy": "empty_query"}
    if not cues:
        return {"source_id": source_id, "source_title": source["title"], "query": query, "results": [], "strategy": "no_transcript_segments"}

    results: list[dict] = []
    for cue in cues:
        hay = cue["text"].lower()
        if needle not in hay:
            continue
        start = cue["start"]
        end = cue["end"]
        results.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "range": f"{format_timecode(start)}-{format_timecode(end)}",
                "text": cue["text"],
            }
        )
        if len(results) >= limit:
            break
    return {
        "source_id": source_id,
        "source_title": source["title"],
        "query": query,
        "results": results,
        "strategy": "transcript_segments",
    }


def run_job(job_id: int) -> None:
    job = next((row for row in db.list_jobs_with_sources() if row["id"] == job_id), None)
    if not job:
        raise RuntimeError(f"job {job_id} tidak ditemukan")

    output_dir = OUTPUTS_DIR / f"job-{job_id}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
    output_dir.mkdir(parents=True, exist_ok=True)
    db.update_job(job_id, status="processing", output_dir=str(output_dir))

    ranges = json.loads(job["clip_ranges_json"])
    try:
        for idx, clip in enumerate(ranges, start=1):
            out_path = output_dir / f"clip-{idx:02d}.mp4"
            input_path, input_start, input_end, cleanup = _resolve_job_input(job, clip)
            try:
                cmd = [
                    ffmpeg_path(),
                    "-y",
                    "-ss",
                    str(input_start),
                    "-to",
                    str(input_end),
                    "-i",
                    input_path,
                    "-c:v",
                    "libx264",
                    "-c:a",
                    "aac",
                    str(out_path),
                ]
                completed = subprocess.run(cmd, capture_output=True, text=True)
                if completed.returncode != 0:
                    raise RuntimeError(completed.stderr[-2000:] or completed.stdout[-2000:] or "ffmpeg gagal")
            finally:
                if cleanup:
                    cleanup()
            db.insert_clip(job_id, idx, clip["start"], clip["end"], str(out_path))
        db.update_job(job_id, status="done", output_dir=str(output_dir), error=None)
    except Exception as exc:
        db.update_job(job_id, status="failed", output_dir=str(output_dir), error=str(exc))
        raise
