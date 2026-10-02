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
| POST | `/api/transactions/export` | Data siap Excel (44 kolom). Body: `{"dateFrom","dateTo","branch"}`, maks 50.000 transaksi |
| GET | `/api/branches` | Daftar branch + jumlah transaksi 65 hari terakhir |

Tanpa `dateFrom`/`dateTo`, rentang default adalah 65 hari terakhir (zona waktu Asia/Jakarta).
Dokumentasi interaktif: `http://187.52.114.14:8002/docs`.

## Konfigurasi

Lihat [.env.example](.env.example). Kredensial database sama dengan yang dipakai engine ESB di VPS (`/opt/esb-integration/.env`).

| Variable | Default | Keterangan |
|----------|---------|------------|
| `DB_HOST` / `DB_PORT` / `DB_NAME` | Supabase session pooler | Koneksi PostgreSQL |
| `DB_USER` / `DB_PASSWORD` | - | Wajib |
| `DB_POOL_MAX` | 5 | Koneksi maksimal per worker (container menjalankan 2 worker) |
| `CORS_ORIGINS` | `*` | Hanya relevan jika browser memanggil backend langsung |
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
