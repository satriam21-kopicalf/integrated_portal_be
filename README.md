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
| GET | `/api/summary` | Gross − Void/Cancelled − Other Cost − Open bill = Sales (per hari & total). Query: `dateFrom`, `dateTo`, `branch`. Hari sebelum kemarin dari agregat harian (1 tahun < 1 dtk) |
| GET | `/api/branches` | Master cabang (nama terkini) + jumlah transaksi Sales 65 hari terakhir |
| POST | `/api/exports` | Mulai job export. Body: `{"dateFrom","dateTo","branch","type","report","format"}` (`format`: `xlsx` default / `gsheet`) → 202 + `id` |
| GET | `/api/exports` | Job export milik user yang login (terbaru dulu) — dipakai dashboard untuk melanjutkan progres di halaman mana pun — plus `googleSheets` (opsi Google Sheets aktif) |
| GET | `/api/exports/{id}` | Status job: `status`, `daysDone/totalDays`, `rows`, `sheets`, `fileSize`, `downloadUrl`; Google Sheets: `phase` (`upload`), `uploadPct`, `sheetUrl`, `sheetSharedWith` |
| GET | `/api/exports/{id}/download` | Unduh `.xlsx` (tersedia `EXPORT_TTL_HOURS`, default 24 jam) |
| GET | `/api/overview/meta` | Opsi filter channel, periode default, cakupan & kesegaran data |
| GET | `/api/overview/kpis` | Sales, Nett Sales, Bills, Avg Ticket + Δ% vs periode sebelumnya + nilai harian |
| GET | `/api/overview/trend` | Seri Sales per `granularity` (`day`/`week`/`month`, default otomatis) + periode sebelumnya + nilai per channel per bucket |
| GET | `/api/overview/channels` | Per channel: bills, sales, share, avg ticket, diskon %, growth + mix per periode |
| GET | `/api/overview/branches` | Leaderboard cabang: sales, bills, avg ticket, growth, void rate, sparkline |
| GET | `/api/overview/hourly` | Hari-dalam-minggu × jam: rata-rata bills & sales per hari |
| GET | `/api/overview/menus` | Top menu (`limit`, `sort=subtotal\|qty`), mix kategori, preferensi add-on |
| GET | `/api/overview/deductions` | Void/Cancelled, Other Cost per metode, open bill; per hari & cabang (status `review` > P90) | Termasuk `groups` (offline vs online), `channels`, `dailyGroups`, `branchGroups`.
| GET | `/api/overview/monthly` | Bulan-bulan dalam periode terpilih: rata-rata per hari, MoM, YoY, same-store growth |
| GET | `/api/overview/payments` | Mix metode pembayaran |
| GET | `/api/overview/basket` | Baris menu & qty per bill, food share, food attach rate |
| GET | `/api/overview/growth` | Sales growth (subtotal): `basis=previous` (default) \| `lastYear` (52 minggu) \| `sequential` (bucket vs bucket sebelumnya, per hari), `granularity`; total, per bucket, per cabang & channel dengan kontribusi (pp) |
| GET | `/api/overview/hourly-compare` | Jam sibuk dibandingkan: `mode=period` (vs `compareFrom`/`compareTo`, default periode sebelumnya) atau `mode=branches` (`compareBranches`, default cabang terpilih / 5 tersibuk, maks. 8): per jam bills, sales, rata-rata per hari, share hari, jam puncak |
| GET | `/api/overview/breakdown` | Penjualan per `by=branch\|channel\|payment\|paymentType\|date\|type`, opsional `paymentMethod`, `txType`; + periode sebelumnya (drill-down) |
| GET | `/api/overview/menu-detail` | Satu menu (`menuId`, `kind`): qty/sales per hari (per bulan bila > 92 hari), per cabang & channel, vs periode sebelumnya |
| WS | `/ws` | Realtime: pesan `hello`/`update` saat data baru tersinkron atau agregat diperbarui, `ping` tiap 25 dtk (lihat [docs/README.md](docs/README.md#5-realtime-websocket)) |
| GET | `/api/realtime/version` | Versi data yang sama via HTTP (fallback bila WebSocket putus) |
| POST | `/api/auth/login` | Login: `{"identifier", "password", "method": "username"\|"email", "remember"}` → cookie sesi HttpOnly |
| POST | `/api/auth/logout` | Logout (sesi dicabut) |
| GET | `/api/auth/me` | User yang sedang login |
| PATCH | `/api/auth/me` | *My profile*: user mengisi profilnya sendiri (`fullName`, `phoneNumber`, `jobTitle`, `department`, `employeeNumber`, `gender`, `birthDate`, `address`, `city`, `workBranchCode`) |
| POST | `/api/auth/password` | Ganti password sendiri `{"currentPassword", "newPassword"}` |
| PUT/DELETE | `/api/auth/me/avatar` | Foto profil sendiri `{"image": "data:image/webp;base64,..."}` |
| GET/POST | `/api/users` | (superadmin) daftar user (`search` — nama/username/email/telepon/jabatan, `role`, `status`, `branch`, `page`, `pageSize`) / buat user (cukup `username`, `email`, `password`, `role`, dan `branches` untuk role user) |
| GET | `/api/users/summary` | (superadmin) jumlah user: total, aktif/nonaktif/terkunci, superadmin/user, tanpa cabang, belum pernah login, login 7 hari, cabang tercakup |
| GET/PATCH/DELETE | `/api/users/{id}` | (superadmin) detail / ubah (termasuk reset `password`) / hapus |
| POST | `/api/users/{id}/unlock` | (superadmin) buka kunci akun |
| PUT/DELETE | `/api/users/{id}/avatar` | (superadmin) foto profil user lain |
| GET | `/api/avatars/{id}` | Foto profil (URL berversi, cache 1 tahun) |
| GET | `/api/cost-control/{meta,summary,trend,items,forecast}` | Cost Control: COGS ratio (net sales & subtotal), usage ratio, selisih stok, waste, estimasi belanja 1/2/4 minggu per outlet (lihat docs) |
| PUT | `/api/cost-control/settings` | (superadmin) ambang status & parameter forecast |
| GET | `/api/activity`, `/api/activity/summary` | (superadmin) Activity log: `dateFrom`, `dateTo`, `user`, `category` (auth, page, filter, transaction, export, user, profile, access), `status` (ok/failed/denied), `role`, `search`, `limit`, `offset` |
| POST | `/api/activity/events` | Dashboard melaporkan `page.view` / `filter.change` (aktivitas lain dicatat backend sendiri) |
| GET | `/api/live` | Penjualan hari ini (vs kemarin di jam yang sama, per jam) + transaksi Sales terbaru yang masuk (`limit`, `branch`, `channel`); langsung dari `transactions_pos_sales`, cache 20 dtk |

Tanpa `dateFrom`/`dateTo`, rentang default adalah 65 hari terakhir (Asia/Jakarta). Dokumentasi interaktif: `https://api.kopicalf.co.id/docs`.
Semua endpoint `/api/overview/*` menerima `compareFrom` & `compareTo` (opsional): periode pembanding pilihan (default: periode sepanjang sama tepat sebelumnya).

**Semua endpoint data membutuhkan login** (cookie `portal_session`, atau `Authorization: Bearer <token>`); `/api/users`, `/api/cost-control/*` dan `GET /api/activity*` hanya untuk role `superadmin`. Terbuka: `/health`, `/api/auth/login|logout`, WebSocket `/ws`.

### Akses cabang (role user, anti-fraud)

- Setiap akun role **user** punya daftar cabang (`integration_portal.user_branch`, migration 008; diatur superadmin di User Accounts, minimal 1 cabang). Superadmin melihat semua cabang.
- Middleware `scope_to_user_branches` (`app/main.py`) mengisi `branch_scope` (`app/scope.py`) dari sesi; semua endpoint data memakai `scoped_branch()`: tanpa filter → semua cabang milik user, filter cabang lain → diabaikan, tidak ada sisa → hasil kosong (tidak pernah "semua"). Berlaku untuk Overview, `/api/transactions`, `/api/summary`, live feed, export, dan `/api/branches` (hanya cabang user). Detail transaksi cabang lain → 404 (dicatat `denied` di activity log).
- Export dimiliki user pembuatnya (`owner`): user lain tidak bisa melihat status/mengunduh (superadmin bisa). Kolom *Generated Username* di file Excel berisi username pembuat export.

### Activity log (migration 009)

`integration_portal.activity_log` (disimpan 400 hari) mencatat: login/logout & login gagal (alasan), ganti password/profil sendiri, halaman dibuka & filter yang dipakai (dari dashboard), detail transaksi dibuka, **export** (diminta dengan periode/cabang/tipe/report, selesai dengan jumlah baris & ukuran file atau gagal beserta error, diunduh), perubahan akun oleh superadmin (field lama → baru, reset password tanpa nilai password), dan akses yang ditolak (halaman/API superadmin, transaksi atau file export milik orang lain). IP diambil dari `X-Forwarded-For` (proxy dashboard). Penulisan log tidak pernah menggagalkan request (`app/activity.py`).
Semua endpoint menerima parameter opsional `v` (versi data dari WebSocket) yang ikut menjadi kunci cache respons.

Dokumentasi lengkap (arsitektur, aturan data, realtime, operasional): [docs/README.md](docs/README.md).

### Aturan data (identik dengan ESB ERP)

`type` membagi transaksi (`transactions_pos_sales`):

| type | Aturan | Arti |
|------|--------|------|
| `sales` (default) | status `Finished` + `bill_num` terisi | = laporan ESB "Sales" |
| `void` | status `Void` / `Cancelled` | pengurangan |
| `other_cost` | status `Finished` tanpa `bill_num` | pengurangan: pembayaran OTHER COST (CUPPING, WASTE, …) |
| `all` | semua | |

Baris laporan dibangun dari `raw_data` (payload ESB) di `app/esb_report.py`: 1 baris per menu + baris `(PACKAGE)`/`(EXTRA)`, Bill Discount dibagi proporsional Subtotal, nama/brand/city cabang dari `master_branches` + `master_branch_attributes` (berdasarkan kode cabang), Waiter dari `master_pos_users`.

Baris menu yang dibatalkan di bill (status `Print Cancelled`) tidak dihitung, sama seperti ESB (subtotal transaksi memang tidak memasukkannya); berlaku untuk export, dashboard, agregat Overview dan live feed.

**Master referensi dari export ESB.** API ESB tidak menyediakan nama user POS (kolom Waiter) maupun City cabang. Keduanya dapat dipelajari dari file export ESB "Sales Recapitulation Detail Report":

```bash
python -m app.reference_import "docs/ESB_Sales Recapitulation Detail Report_01 Oktober 2026.xlsx" [file lain ...]           # dry run
python -m app.reference_import "docs/ESB_Sales Recapitulation Detail Report_01 Oktober 2026.xlsx" [file lain ...] --apply   # tulis
```

Tool ini mencocokkan baris export ESB dengan transaksi kita lewat Sales Number, lalu mengisi/memperbarui `master_pos_users` dan `master_branch_attributes` (brand, city, area). Jalankan bila ada outlet atau kasir baru.

Validasi (Sep 2026): Subtotal Sales per hari = ERP ESB 30/30 hari; export Sales Recapitulation Detail 10 Sep identik dengan file ESB (46 kolom; kecuali Custom Menu Name yang tidak tersedia di API); Daily Sales Recapitulation 1–29 Sep identik (3.028 baris, 0 selisih).

### Export Excel

- `report=detail` → **Sales Recapitulation Detail Report** (46 kolom ESB). `report=daily` → **Daily Sales Recapitulation Report** (per tanggal × cabang, 20 kolom).
- Layout sama dengan file ESB (judul, Period, Branch, Sales Type, header di baris 11/12), tanggal sebagai tanggal Excel, sheet tambahan **Ringkasan** (Gross − pengurangan = Sales per hari).
- Setiap export berjalan di **proses terpisah** (`app/export_worker.py`), bukan di worker API, sehingga export besar tidak memperlambat/mematikan worker; maksimal `EXPORT_MAX_CONCURRENT` export bersamaan (sisanya `queued`). Bila proses export berhenti, status langsung menjadi `error`.
- Tanpa batas rentang: data dibaca per hari dan ditulis streaming (`app/xlsx_stream.py`); > 1.048.575 baris otomatis lanjut ke sheet `Report (2)`, dst.
- Acuan di VPS: detail 1 hari ≈ 65 rb baris ≈ 9 dtk, 1 bulan ≈ 1,9 jt baris ≈ 5 menit; daily 1 bulan ≈ 40 dtk.

### Export Google Sheets (`format=gsheet`)

- File `.xlsx` yang sama di-upload ke Google Drive dan dikonversi menjadi Google Sheet (`app/gsheets.py`), disimpan di folder `GOOGLE_DRIVE_FOLDER_ID` dan dibagikan ke email user yang melakukan export (`GOOGLE_SHARE_ROLE`, default `writer`). File `.xlsx` tetap bisa diunduh.
- Batas Google Sheets 10 juta sel: detail 46 kolom ≈ 217 rb baris (± 3 hari seluruh outlet). Export yang melewati batas berhenti lebih awal dengan pesan error; gunakan Excel atau perkecil periode/cabang.
- Opsi hanya muncul di dashboard bila kredensial terpasang (salah satu):
  - **Akun Google (Gmail biasa)**: Google Cloud project → aktifkan *Google Drive API* → OAuth consent screen *External*, status **In production** (mode *Testing* membuat token kedaluwarsa 7 hari) → Credentials → OAuth client *Desktop app* → unduh JSON → jalankan di komputer sendiri `python scripts/google_oauth_setup.py client_secret.json` → salin 4 baris yang dicetak ke `.env` server.
  - **Service account**: `GOOGLE_SERVICE_ACCOUNT_JSON_B64` (key JSON di-base64) + folder di **Shared Drive** (Google Workspace) dengan service account sebagai anggota; service account tidak punya kuota Drive sendiri.

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
| `WS_ALLOWED_ORIGINS` | portal, preview Vercel, localhost:3002 | Origin yang boleh membuka WebSocket `/ws` |
| `PUBLIC_BASE_URL` | kosong | Basis URL absolut untuk link unduhan export |
| `EXPORT_TTL_HOURS` | 24 | Lama file export disimpan |
| `EXPORT_MAX_CONCURRENT` | 2 | Job export bersamaan (seluruh container) |
| `GOOGLE_DRIVE_FOLDER_ID` | kosong | Folder Drive tujuan export Google Sheets (kosong = opsi nonaktif) |
| `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` / `_REFRESH_TOKEN` | kosong | Akun Google pemilik file (dari `scripts/google_oauth_setup.py`) |
| `GOOGLE_SERVICE_ACCOUNT_JSON_B64` | kosong | Alternatif: service account + Shared Drive |
| `GOOGLE_SHARE_ROLE` | `writer` | Akses user pembuat export ke sheet (`writer`/`reader`) |
| `TIMEZONE` | `Asia/Jakarta` | Untuk rentang tanggal default |
| `OVERVIEW_DATA_FROM` | `2025-08-01` | Awal riwayat lengkap untuk perbandingan Overview |
| `SESSION_HOURS` / `SESSION_REMEMBER_DAYS` | 12 / 30 | Masa berlaku sesi login (biasa / "keep me signed in") |

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
