# Clipper Personal MVP

MVP web app untuk use pribadi:
- upload video lokal
- ingest source dari URL YouTube via `yt-dlp`
- preview metadata + transcript subtitle sebelum download
- simpan source ke SQLite
- tampilkan metadata dasar source (durasi, channel, URL, subtitle)
- simpan transcript subtitle jika tersedia dan bisa diambil
- auto-suggest clip ranges dari transcript cues atau fallback duration windows
- upload/hapus global `cookies.txt` untuk request YouTube via `yt-dlp`
- optional AI rerank untuk suggestion jika env OpenAI-compatible diset; default otomatis pakai Meow Labs (`MEOWLABS_API_KEY` -> `https://api.meowlabs.store/v1`, model `kr/glm-5`) dengan retry + timeout fallback saat gateway ngadat, plus cache suggestion 180 detik (persist ke SQLite, auto-clear saat cookies berubah)
- badge warna di UI untuk ai/cache/provider status (`applied`, `fallback`, `hit-memory`, `hit-sqlite`, `miss`)
- Phase 6 picker: pilih suggestion via checkbox, edit start/end, lalu generate job dari selection
- cari keyword dalam transcript per source
- buat clip job manual dengan range waktu
- hasil clip tersimpan lokal dan bisa diunduh dari browser
- source YouTube baru disimpan dalam mode **transcript-first / remote-section**: tidak download full video saat ingest, clip diambil per-range saat job jalan

## Run

```bash
cd /home/meowlabs/clipper-personal-mvp
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Buka `http://127.0.0.1:8765`.
