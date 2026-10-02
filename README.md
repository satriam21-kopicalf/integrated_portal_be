# Integrated Portal Backend (integrated_portal_be)

Backend API (Python FastAPI) untuk [integrated_portal](https://github.com/satriam21-kopicalf/integrated_portal).
Menyajikan data transaksi POS ESB dari Supabase PostgreSQL (schema `integration_esb`).

```
Browser ──> Next.js (Vercel) ──/api/* rewrite──> integrated_portal_be (VPS :8002) ──> Supabase PostgreSQL
```

Format request/response sama persis dengan Next.js API routes lama, sehingga komponen frontend tidak perlu diubah.

## Endpoints

| Method | Path | Keterangan |
|--------|------|------------|
| GET | `/health` | Status service + koneksi database |
| GET | `/api/transactions` | List transaksi (header + item), cursor pagination. Query: `limit` (≤100), `cursor`, `search`, `dateFrom`, `dateTo`, `branch`, `cache=false` |
| GET | `/api/transactions/{sales_num}` | Detail satu transaksi beserta `items` |
| GET | `/api/branches` | Daftar branch + jumlah transaksi 65 hari terakhir |
| POST | `/api/exports` | Mulai job export Excel. Body: `{"dateFrom","dateTo","branch"}` → 202 + `id` |
| GET | `/api/exports/{id}` | Status job: `status`, `daysDone/totalDays`, `rows`, `sheets`, `fileSize`, `downloadUrl` |
| GET | `/api/exports/{id}/download` | Unduh file `.xlsx` (tersedia `EXPORT_TTL_HOURS`, default 24 jam) |

Tanpa `dateFrom`/`dateTo`, rentang default adalah 65 hari terakhir (zona waktu Asia/Jakarta).

### Export Excel

- Tidak ada batas rentang. Data dibaca per hari (terbaru dulu) dan ditulis streaming oleh `app/xlsx_stream.py`.
- Format: sheet `Summary` + `Transactions` (44 kolom, satu baris per item). Jika lebih dari 1.048.575 baris, otomatis lanjut ke `Transactions (2)`, dst.
- Acuan performa di VPS: 1 hari ≈ 65 rb baris ≈ 9 dtk; September 2026 (740.762 transaksi, 1,88 jt baris) ≈ 4,6 menit, 258 MB.
- Link unduhan relatif (lewat proxy Vercel) kecuali `PUBLIC_BASE_URL` di-set, misalnya `https://portal-api.kopicalf.co.id`, agar browser mengunduh langsung dari backend.
Dokumentasi interaktif: `http://187.52.114.14:8002/docs`.

## Konfigurasi

Lihat [.env.example](.env.example). Kredensial database sama dengan yang dipakai engine ESB di VPS (`/opt/esb-integration/.env`).

| Variable | Default | Keterangan |
|----------|---------|------------|
| `DB_HOST` / `DB_PORT` / `DB_NAME` | Supabase session pooler | Koneksi PostgreSQL |
| `DB_USER` / `DB_PASSWORD` | - | Wajib |
| `DB_POOL_MAX` | 5 | Koneksi maksimal per worker (container menjalankan 2 worker) |
| `CORS_ORIGINS` | `*` | Hanya relevan jika browser memanggil backend langsung |
| `PUBLIC_BASE_URL` | kosong | Basis URL absolut untuk link unduhan export |
| `EXPORT_TTL_HOURS` | 24 | Lama file export disimpan |
| `EXPORT_MAX_CONCURRENT` | 2 | Job export bersamaan per worker |
| `TIMEZONE` | `Asia/Jakarta` | Untuk rentang tanggal default |

## Development

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env    # isi DB_PASSWORD
uvicorn app.main:app --reload --port 8002
```

Test (database di-stub, tidak butuh koneksi DB):

```bash
pytest -q
```

## Deploy ke VPS

```bash
bash scripts/deploy.sh
```

Script ini mengirim source ke `/opt/integrated-portal-be` lewat SSH, lalu menjalankan `docker compose up -d --build` dan menunggu `/health`.
Pada deploy pertama, `.env` di server dibuat dari kredensial `DB_*` di `/opt/esb-integration/.env`. Deploy berikutnya tidak menimpa `.env` tersebut.

Operasional di VPS:

```bash
cd /opt/integrated-portal-be
docker compose ps
docker logs --tail 100 -f integrated-portal-be
docker compose restart
```
