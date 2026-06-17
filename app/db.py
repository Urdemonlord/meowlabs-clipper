from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "app.db"


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                kind TEXT NOT NULL,
                input_path TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                clip_ranges_json TEXT NOT NULL,
                output_dir TEXT,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(source_id) REFERENCES sources(id)
            );

            CREATE TABLE IF NOT EXISTS clips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                clip_index INTEGER NOT NULL,
                start_seconds REAL NOT NULL,
                end_seconds REAL NOT NULL,
                output_path TEXT NOT NULL,
                duration_seconds REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(job_id) REFERENCES jobs(id)
            );

            CREATE TABLE IF NOT EXISTS suggestion_cache (
                cache_key TEXT PRIMARY KEY,
                source_id INTEGER,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY(source_id) REFERENCES sources(id)
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def insert_source(title: str, kind: str, input_path: str, metadata: dict | None = None) -> int:
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO sources (title, kind, input_path, metadata_json) VALUES (?, ?, ?, ?)",
            (title, kind, input_path, json.dumps(metadata or {})),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_sources() -> list[sqlite3.Row]:
    conn = connect()
    try:
        return conn.execute("SELECT * FROM sources ORDER BY id DESC").fetchall()
    finally:
        conn.close()


def update_source_metadata(source_id: int, metadata: dict) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE sources SET metadata_json = ? WHERE id = ?",
            (json.dumps(metadata), source_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_source(source_id: int) -> sqlite3.Row | None:
    conn = connect()
    try:
        return conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    finally:
        conn.close()


def insert_job(source_id: int, clip_ranges: Iterable[dict]) -> int:
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO jobs (source_id, status, clip_ranges_json) VALUES (?, 'queued', ?)",
            (source_id, json.dumps(list(clip_ranges))),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def update_job(job_id: int, *, status: str, output_dir: str | None = None, error: str | None = None) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE jobs SET status = ?, output_dir = COALESCE(?, output_dir), error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, output_dir, error, job_id),
        )
        conn.commit()
    finally:
        conn.close()


def insert_clip(job_id: int, clip_index: int, start_seconds: float, end_seconds: float, output_path: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO clips (job_id, clip_index, start_seconds, end_seconds, output_path, duration_seconds) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, clip_index, start_seconds, end_seconds, output_path, round(end_seconds - start_seconds, 3)),
        )
        conn.commit()
    finally:
        conn.close()


def list_jobs_with_sources() -> list[sqlite3.Row]:
    conn = connect()
    try:
        return conn.execute(
            """
            SELECT jobs.*, sources.title AS source_title, sources.input_path AS source_path
            FROM jobs
            JOIN sources ON sources.id = jobs.source_id
            ORDER BY jobs.id DESC
            """
        ).fetchall()
    finally:
        conn.close()


def list_clips_for_job(job_id: int) -> list[sqlite3.Row]:
    conn = connect()
    try:
        return conn.execute("SELECT * FROM clips WHERE job_id = ? ORDER BY clip_index ASC", (job_id,)).fetchall()
    finally:
        conn.close()


def get_suggestion_cache(cache_key: str) -> sqlite3.Row | None:
    conn = connect()
    try:
        return conn.execute("SELECT * FROM suggestion_cache WHERE cache_key = ?", (cache_key,)).fetchone()
    finally:
        conn.close()


def upsert_suggestion_cache(cache_key: str, source_id: int | None, payload: dict, created_at: float) -> None:
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO suggestion_cache (cache_key, source_id, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                source_id = excluded.source_id,
                payload_json = excluded.payload_json,
                created_at = excluded.created_at
            """,
            (cache_key, source_id, json.dumps(payload), created_at),
        )
        conn.commit()
    finally:
        conn.close()


def delete_suggestion_cache(cache_key: str | None = None, source_id: int | None = None) -> None:
    conn = connect()
    try:
        if cache_key is not None:
            conn.execute("DELETE FROM suggestion_cache WHERE cache_key = ?", (cache_key,))
        elif source_id is not None:
            conn.execute("DELETE FROM suggestion_cache WHERE source_id = ?", (source_id,))
        else:
            conn.execute("DELETE FROM suggestion_cache")
        conn.commit()
    finally:
        conn.close()
