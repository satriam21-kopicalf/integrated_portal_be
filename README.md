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
| GET | `/api/transactions` | Baris laporan ESB per item, cursor pagination. Query: `limit` (≤100), `cursor`, `search`, `dateFrom`, `dateTo`, `branch` (kode cabang), `type`, `cache=false` |
| GET | `/api/transactions/{sales_num}` | Detail satu transaksi (`items` + `report_rows`) |
| GET | `/api/summary` | Gross − Void/Cancelled − Other Cost − Open bill = Sales (per hari & total). Query: `dateFrom`, `dateTo`, `branch` |
| GET | `/api/branches` | Master cabang (nama terkini) + jumlah transaksi Sales 65 hari terakhir |
| POST | `/api/exports` | Mulai job export. Body: `{"dateFrom","dateTo","branch","type","report"}` → 202 + `id` |
| GET | `/api/exports/{id}` | Status job: `status`, `daysDone/totalDays`, `rows`, `sheets`, `fileSize`, `downloadUrl` |
| GET | `/api/exports/{id}/download` | Unduh `.xlsx` (tersedia `EXPORT_TTL_HOURS`, default 24 jam) |

Tanpa `dateFrom`/`dateTo`, rentang default adalah 65 hari terakhir (Asia/Jakarta). Dokumentasi interaktif: `http://187.52.114.14:8002/docs`.

### Aturan data (identik dengan ESB ERP)

`type` membagi transaksi (`transactions_pos_sales`):

| type | Aturan | Arti |
|------|--------|------|
| `sales` (default) | status `Finished` + `bill_num` terisi | = laporan ESB "Sales" |
| `void` | status `Void` / `Cancelled` | pengurangan |
| `other_cost` | status `Finished` tanpa `bill_num` | pengurangan: pembayaran OTHER COST (CUPPING, WASTE, …) |
| `all` | semua | |

Baris laporan dibangun dari `raw_data` (payload ESB) di `app/esb_report.py`: 1 baris per menu + baris `(PACKAGE)`/`(EXTRA)`, Bill Discount dibagi proporsional Subtotal, nama/brand/city cabang dari `master_branches` + `master_branch_attributes` (berdasarkan kode cabang), Waiter dari `master_pos_users`.

Validasi (Sep 2026): Subtotal Sales per hari = ERP ESB 30/30 hari; export Sales Recapitulation Detail 10 Sep identik dengan file ESB (46 kolom; kecuali Custom Menu Name yang tidak tersedia di API); Daily Sales Recapitulation 1–29 Sep identik (3.028 baris, 0 selisih).

### Export Excel

- `report=detail` → **Sales Recapitulation Detail Report** (46 kolom ESB). `report=daily` → **Daily Sales Recapitulation Report** (per tanggal × cabang, 20 kolom).
- Layout sama dengan file ESB (judul, Period, Branch, Sales Type, header di baris 11/12), tanggal sebagai tanggal Excel, sheet tambahan **Ringkasan** (Gross − pengurangan = Sales per hari).
- Tanpa batas rentang: data dibaca per hari dan ditulis streaming (`app/xlsx_stream.py`); > 1.048.575 baris otomatis lanjut ke sheet `Report (2)`, dst.
- Acuan di VPS: detail 1 hari ≈ 65 rb baris ≈ 9 dtk, 1 bulan ≈ 1,9 jt baris ≈ 5 menit; daily 1 bulan ≈ 40 dtk.

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
