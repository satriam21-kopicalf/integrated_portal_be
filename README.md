# Integrated Portal Backend (integrated_portal_be)

Backend API (Python FastAPI) untuk [integrated_portal](https://github.com/satriam21-kopicalf/integrated_portal).
Menyajikan data transaksi POS ESB dari Supabase PostgreSQL (schema `integration_esb`, milik engine ESB — hanya dibaca).
Tabel milik portal sendiri (agregat Overview, nantinya juga akun user) ada di schema **`integration_portal`**.

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
| GET | `/api/overview/meta` | Opsi filter channel, periode default, cakupan & kesegaran data |
| GET | `/api/overview/kpis` | Sales, Nett Sales, Bills, Avg Ticket + Δ% vs periode sebelumnya + nilai harian |
| GET | `/api/overview/trend` | Seri Sales per `granularity` (`day`/`week`/`month`, default otomatis) + periode sebelumnya |
| GET | `/api/overview/channels` | Per channel: bills, sales, share, avg ticket, diskon %, growth + mix per periode |
| GET | `/api/overview/branches` | Leaderboard cabang: sales, bills, avg ticket, growth, void rate, sparkline |
| GET | `/api/overview/hourly` | Hari-dalam-minggu × jam: rata-rata bills & sales per hari |
| GET | `/api/overview/menus` | Top menu (`limit`, `sort=subtotal\|qty`), mix kategori, preferensi add-on |
| GET | `/api/overview/deductions` | Void/Cancelled, Other Cost per metode, open bill; per hari & cabang (status `review` > P90) |
| GET | `/api/overview/monthly` | Sales bulanan (`months`, default 13): MoM, YoY, same-store growth |
| GET | `/api/overview/payments` | Mix metode pembayaran |
| GET | `/api/overview/basket` | Baris menu & qty per bill, food share, food attach rate |
| GET | `/api/live` | Penjualan hari ini (vs kemarin di jam yang sama, per jam) + transaksi Sales terbaru yang masuk (`limit`, `branch`, `channel`); langsung dari `transactions_pos_sales`, cache 20 dtk |

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
- Setiap export berjalan di **proses terpisah** (`app/export_worker.py`), bukan di worker API, sehingga export besar tidak memperlambat/mematikan worker; maksimal `EXPORT_MAX_CONCURRENT` export bersamaan (sisanya `queued`). Bila proses export berhenti, status langsung menjadi `error`.
- Tanpa batas rentang: data dibaca per hari dan ditulis streaming (`app/xlsx_stream.py`); > 1.048.575 baris otomatis lanjut ke sheet `Report (2)`, dst.
- Acuan di VPS: detail 1 hari ≈ 65 rb baris ≈ 9 dtk, 1 bulan ≈ 1,9 jt baris ≈ 5 menit; daily 1 bulan ≈ 40 dtk.

### Overview (schema `integration_portal`)

Endpoint `/api/overview/*` menerima `dateFrom`, `dateTo` (default 30 hari lengkap s/d kemarin), `branch` (kode) dan `channel` (dipisah koma). Periode pembanding = jumlah hari yang sama tepat sebelum `dateFrom`; bila menjangkau sebelum `OVERVIEW_DATA_FROM` (2025-08-01, roll-out ESB baru lengkap di seluruh cabang akhir Juli 2025) pembanding dikosongkan (`deltaPct: null`). Angka = ESB "Sales", sehingga total sama dengan `/api/summary` dan laporan ESB. Respons di-cache 5 menit.

Data dibaca dari tabel agregat (bukan `raw_data`), dibangun ulang per tanggal oleh `app/aggregates.py`:

| Tabel | Grain |
|---|---|
| `agg_sales_daily` | tanggal × cabang × channel × metode bayar × `tx_type` (bills, subtotal, nett, diskon, basket) |
| `agg_sales_hourly` | tanggal × cabang × channel × jam (`salesDateIn`), hanya Sales |
| `agg_menu_daily` / `agg_menu_monthly` | tanggal/bulan × cabang × channel × menu × `kind` (menu/package/extra) |
| `agg_refresh_log` | 1 baris per tanggal: waktu refresh, `synced_at` sumber, rekonsiliasi subtotal |

- **Migration**: file bernomor di `app/migrations/*.sql`, dijalankan otomatis setelah deploy (`python -m app.migrate`, riwayat di `integration_portal.schema_migrations`).
- **Refresh**: cron `scripts/aggregates.cron` (dipasang otomatis ke `/etc/cron.d/integrated-portal-aggregates`) — tiap jam menit :20 (hari ini + kemarin, setelah sinkron ESB menit :05) dan 02:50 WIB (8 hari, setelah resync 7 hari). Log: `/var/log/integrated-portal-aggregates.log`.
- Setiap tanggal di-rebuild dalam satu transaksi lalu direkonsiliasi: Σ subtotal Sales agregat harus sama dengan `transactions_pos_sales`.
- **Backfill / rebuild manual** di VPS: `bash /opt/integrated-portal-be/repo/scripts/aggregates.sh --from 2025-06-09 --to 2026-10-03` (≈ 2 dtk per hari).

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
| `EXPORT_MAX_CONCURRENT` | 2 | Job export bersamaan (seluruh container) |
| `TIMEZONE` | `Asia/Jakarta` | Untuk rentang tanggal default |
| `OVERVIEW_DATA_FROM` | `2025-08-01` | Awal riwayat lengkap untuk perbandingan Overview |

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

**Otomatis:** setiap push ke `main` di-deploy oleh VPS dalam ≤ 5 menit (`scripts/auto-deploy.sh`, dijalankan cron `/etc/cron.d/integrated-portal-be-deploy`): test → build → restart → cek `/health` (rollback otomatis bila gagal) → migration `integration_portal` → pasang cron agregat. Log: `/var/log/integrated-portal-be-deploy.log`.

**Manual** (dari komputer lokal):

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
