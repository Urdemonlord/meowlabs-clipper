# Clipper

Clipper adalah web app ringan untuk bikin clip dari source lokal atau YouTube dengan alur **transcript-first**. Fokusnya bukan editor video penuh, tapi panel kerja cepat untuk:

- ingest source YouTube tanpa download full video saat awal
- ambil transcript/subtitle dulu kalau tersedia
- generate suggestion clip
- optional AI rerank via Meow Labs
- potong clip final per-range saat job dijalankan

## Nama & positioning

- **Nama produk:** `Clipper`
- **Domain target:** `clipper.meowlabs.id`
- **Repo:** `meowlabs-clipper`

Bukan nama AI-slop; cukup pendek, jelas, dan langsung nyambung ke fungsinya.

## Fitur inti

- upload video lokal
- ingest URL YouTube via `yt-dlp`
- preview metadata + transcript sebelum simpan source
- storage mode **transcript-first / remote-section** untuk source YouTube
- manual clip job dari range waktu
- transcript search per source
- suggestion clip dari transcript cues
- optional AI rerank suggestion via Meow Labs (`kr/glm-5`)
- retry + timeout fallback kalau gateway AI lambat/ngadat
- suggestion cache 180 detik (memory + SQLite)
- cache auto-clear saat `cookies.txt` berubah
- badge UI untuk `applied`, `fallback`, `miss`, `hit-memory`, `hit-sqlite`

## Runtime requirements

Minimal host VPS:

- Python 3.11+
- `uv`
- `ffmpeg`
- `yt-dlp`
- Linux VPS

## Environment

Buat `.env` dari contoh:

```bash
cp .env.example .env
```

Isi minimal:

```env
MEOWLABS_API_KEY=replace-me
CLIPPER_SUGGESTION_CACHE_TTL=180
```

## Run lokal / VPS tanpa Docker

```bash
cd /home/meowlabs/clipper-personal-mvp
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Health check:

```bash
curl http://127.0.0.1:8765/health
```

## Deploy via systemd

File unit sudah disiapkan di:

```text
deploy/systemd/clipper.service
```

Pasang:

```bash
sudo cp deploy/systemd/clipper.service /etc/systemd/system/clipper.service
sudo systemctl daemon-reload
sudo systemctl enable --now clipper
sudo systemctl status clipper
```

Log:

```bash
journalctl -u clipper -f
```

## Deploy via Docker Compose

File Compose sudah disiapkan di root repo.

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8765/health
```

Path yang dipersist:

- `./data`
- `./uploads`
- `./outputs`
- `./transcripts`
- `./cookies`
- `./tmp`

## Domain `clipper.meowlabs.id`

Nginx config sudah disiapkan di:

```text
deploy/nginx/clipper.meowlabs.id.conf
```

Pasang:

```bash
sudo cp deploy/nginx/clipper.meowlabs.id.conf /etc/nginx/sites-available/clipper.meowlabs.id.conf
sudo ln -sf /etc/nginx/sites-available/clipper.meowlabs.id.conf /etc/nginx/sites-enabled/clipper.meowlabs.id.conf
sudo nginx -t
sudo systemctl reload nginx
```

Lalu arahkan DNS:

- `clipper.meowlabs.id` -> A record ke IP VPS

Kalau mau HTTPS:

```bash
sudo certbot --nginx -d clipper.meowlabs.id
```

## Repo hygiene

Sengaja tidak di-commit:

- `.env`
- `data/`
- `uploads/`
- `outputs/`
- `transcripts/`
- `cookies/`
- `tmp/`

## Status verifikasi saat ini

Sudah diverifikasi:

- `/health` 200 OK
- AI suggest live jalan
- provider Meow Labs tampil di UI
- cache `miss -> hit-memory -> hit-sqlite`
- cache invalidate saat cookies upload/delete
- syntax compile pass

Belum diverifikasi dari README ini:

- reverse proxy Nginx live di domain publik
- sertifikat TLS live
- deploy Compose di mesin kosong dari nol

## Repo lokal

Folder kerja lokal saat ini masih:

```text
/home/meowlabs/clipper-personal-mvp
```

Nama folder lokal belum wajib diganti untuk deploy; yang penting branding app, repo, dan domain sudah rapi.
