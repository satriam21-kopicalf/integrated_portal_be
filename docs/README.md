# integrated_portal_be — Dokumentasi Backend

Backend FastAPI untuk dashboard [integrated_portal](https://github.com/satriam21-kopicalf/integrated_portal).
Ringkasan cepat (endpoint, konfigurasi, development) ada di [README.md](../README.md); dokumen ini menjelaskan arsitektur, aturan data, realtime dan operasional.

> File `docs/*.xlsx` (contoh export ESB/portal untuk validasi) berisi data pelanggan dan **tidak** di-commit.

## 1. Arsitektur

```
Browser ── portal.kopicalf.co.id (Vercel, Next.js) ── /api/* proxy ──────────────┐
   │                                                                              ▼
   └── wss://api.kopicalf.co.id/ws ── Cloudflare ── Traefik (VPS :443) ── integrated_portal_be :8002 (Docker, 2 worker uvicorn)
                                                                                  │
                         integrated-esbapi (engine sinkron ESB, cron) ──► Supabase PostgreSQL
                                                                         ├─ integration_esb     (milik engine; dibaca)
                                                                         └─ integration_portal  (milik portal; agregat, nanti akun user)
```

- **api.kopicalf.co.id** → Cloudflare (proxied) → Traefik (`/docker/traefik-gexn/api-router.yml`, sertifikat Let's Encrypt) → `127.0.0.1:8002`. Melayani REST, unduhan export, Swagger (`/docs`) dan WebSocket (`/ws`). Proxy Vercel tidak bisa meneruskan WebSocket, karena itu browser terhubung langsung ke subdomain ini.
- Data POS ditarik engine `integrated-esbapi`: tiap jam menit :05 (hari ini + kemarin), resync 7 hari 02:15 WIB, master 01:30 WIB.

## 2. Aturan data (sama dengan laporan ESB)

| Tipe | Aturan (`transactions_pos_sales`) |
|---|---|
| `sales` | status `Finished` dan `bill_num` terisi — sama dengan laporan *Sales Recapitulation* |
| `void` | status `Void` / `Cancelled` |
| `other_cost` | `Finished` tanpa bill number (CUPPING, WASTE, …) |
| `open` | lainnya |

- Baris laporan dibangun dari `raw_data` (`app/esb_report.py`): 1 baris per menu + baris `(PACKAGE)` / `(EXTRA)`; bill discount dibagi proporsional subtotal.
- **Baris menu yang dibatalkan** (`statusName` *Print Cancelled*, `statusID` 19) tidak dihitung di mana pun (export, dashboard, agregat, live) — subtotal transaksi memang tidak memasukkannya.
- Nama/brand/city cabang dari `master_branches` + `master_branch_attributes`; Waiter dari `master_pos_users`. Keduanya bisa dilengkapi dari export ESB: `python -m app.reference_import <file ESB.xlsx> [--apply]`.
- Validasi terhadap export ESB (1–2 Okt 2026): jumlah transaksi, baris dan semua kolom nilai identik; City & Waiter 100%. Tidak bisa direproduksi dari API: *Custom Menu Name* (±4%), *Order Time* baris package (selisih 1 detik, ±0,7%).

## 3. Schema `integration_portal`

| Tabel | Isi |
|---|---|
| `agg_sales_daily` | per tanggal × cabang × channel × metode bayar × tipe: bills, subtotal, nett, grand total, diskon, basket |
| `agg_sales_hourly` / `agg_hourly_monthly` | per jam (dan rollup bulanan per hari-dalam-minggu × jam) |
| `agg_menu_daily` / `agg_menu_monthly` | per menu × kind (menu/package/extra) |
| `agg_refresh_log` | 1 baris per tanggal: waktu refresh, `synced_at` sumber, rekonsiliasi subtotal |
| `user_account` | akun login dashboard (lihat §8) |
| `user_session` | sesi login (hash token, kedaluwarsa, dicabut) |
| `user_avatar` | foto profil (webp/jpeg/png, maks. 512 KB) |
| `agg_cost_period` | Cost Control per cabang × periode opname: penjualan, nilai stok awal/akhir, pembelian, COGS teoretis & aktual, pemakaian lain, selisih opname (diposting & tertunda), jumlah opname |
| `agg_cost_item_period` | idem per item (qty & nilai) |
| `cost_settings` | ambang status (COGS, usage, gap, waste) & parameter forecast |
| `schema_migrations` | migration yang sudah dijalankan (`app/migrations/*.sql`, otomatis setelah deploy) |

Agregat dibangun ulang per tanggal oleh `app/aggregates.py` dan direkonsiliasi dengan data mentah. Cron VPS `/etc/cron.d/integrated-portal-aggregates`: tiap jam menit :20 (hari ini + kemarin) dan 02:50 WIB (8 hari).

Agregat Cost Control dibangun oleh `app/cost_control.py` (`scripts/cost_control.sh --recent-days 10` tiap 05:10 WIB setelah sinkron inventory valuation ESB, `--recent-days 40` Minggu 06:00 WIB). Sumber (`integration_esb`, engine `integrated-esbapi`): `inventory_valuation` (stok awal/akhir, pembelian, pemakaian penjualan × BOM = COGS teoretis, item journal, opname yang diposting), dokumen `stock_opname` yang belum diposting (selisih = Σ(qty fisik − qty sistem) × HPP), dan `agg_sales_daily`. **COGS aktual = teoretis + pemakaian lain + manufacturing − selisih opname** (selisih negatif = kehilangan).

## 4. Endpoint

| Endpoint | Keterangan |
|---|---|
| `GET /health` | status + koneksi DB |
| `GET /api/transactions`, `/api/transactions/{sales_num}` | daftar (cursor pagination, rentang bebas) dan detail |
| `GET /api/summary` | Gross − Void − Other Cost − Open = Sales per hari & total. **Hari sebelum kemarin dibaca dari `agg_sales_daily`** (hasil identik, 1 tahun ±0,5 s; sebelumnya 74 s); hari ini/kemarin dan hari yang belum diagregasi dibaca mentah |
| `GET /api/branches` | master cabang + jumlah transaksi 65 hari |
| `GET /api/overview/*` | 11 endpoint Overview (meta, kpis, trend, channels, branches, hourly, menus, deductions, monthly, payments, basket); filter `dateFrom`, `dateTo`, `branch` (satu kode atau beberapa dipisah koma), `channel`. `monthly` menampilkan bulan-bulan di dalam periode terpilih |
| `GET /api/live` | hari ini: total, per jam, per channel, vs kemarin di jam yang sama (sales, bills, nett), total & per jam kemarin, batch sinkron terakhir, transaksi terbaru (`limit`, `branch`, `channel`) |
| `POST /api/exports`, `GET /api/exports/{id}`, `/download` | export Excel (proses terpisah per job, maks. `EXPORT_MAX_CONCURRENT`) |
| `GET /api/cost-control/meta`, `/summary`, `/trend`, `/items`, `/forecast` | Cost Control: rasio pada net sales & subtotal, status per ambang, median outlet; tren per periode/bulan; item (usage ratio, selisih); estimasi belanja 7/14/30 hari (pemakaian 28 hari terakhir × tren penjualan ±20% + safety stock 2 hari − stok buku, harga HPP). Filter `dateFrom`, `dateTo`, `branch` |
| `PUT /api/cost-control/settings` | (superadmin) ambang status & parameter forecast |
| `WS /ws`, `GET /api/realtime/version` | realtime (lihat §5) |
| `/api/auth/*`, `/api/users/*`, `/api/avatars/*` | login, akun & foto profil (lihat §8) |

## 5. Realtime (WebSocket)

`app/routes/realtime.py`

- Server memantau dua *version stamp* tiap 15 detik (hanya selama ada klien):
  - `salesSyncedAt` = `max(synced_at)` transaksi hari ini & kemarin (berubah setiap sinkron POS);
  - `aggregatesRefreshedAt` = `max(refreshed_at)` di `agg_refresh_log` (berubah setiap refresh agregat).
- Pesan JSON:
  - saat terhubung: `{"type":"hello","salesSyncedAt","aggregatesRefreshedAt","version"}`
  - saat berubah: `{"type":"update","changed":["salesSyncedAt"],...}`
  - tiap 25 detik: `{"type":"ping"}` (menjaga koneksi melewati Cloudflare/Traefik); klien boleh kirim `"ping"` → `{"type":"pong"}`.
- Server tidak mengirim data; klien mengambil ulang lewat REST dengan filternya sendiri.
- **Cache versi**: parameter `v` (versi yang diterima klien) menjadi bagian kunci cache respons (`data_version` di `app/utils.py`, diset middleware), sehingga request setelah update tidak pernah mendapat respons cache lama.
- **Origin**: hanya `WS_ALLOWED_ORIGINS` (pola wildcard) yang diterima; lainnya ditolak (403).
- Fallback HTTP: `GET /api/realtime/version` (payload sama, cache 10 detik) dipakai frontend saat WebSocket terputus.
- Setiap worker uvicorn menjalankan watcher-nya sendiri (2 query `max()` ringan per 15 detik).

## 6. Konfigurasi (env)

| Variable | Default | Keterangan |
|---|---|---|
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | Supabase pooler | koneksi PostgreSQL |
| `WS_ALLOWED_ORIGINS` | `https://portal.kopicalf.co.id,https://integrated-portal*.vercel.app,http://localhost:3002,http://127.0.0.1:3002` | origin WebSocket |
| `PUBLIC_BASE_URL` | kosong | basis URL absolut link unduhan export (mis. `https://api.kopicalf.co.id`) |
| `EXPORT_MAX_CONCURRENT`, `EXPORT_TTL_HOURS` | 2, 24 | export |
| `OVERVIEW_DATA_FROM` | `2025-08-01` | awal riwayat lengkap untuk perbandingan |
| `SESSION_HOURS`, `SESSION_REMEMBER_DAYS` | 12, 30 | masa berlaku sesi (biasa / "keep me signed in") |

## 7. Operasional

- **Deploy**: push ke `main` → VPS (`scripts/auto-deploy.sh`, cron 5 menit): test → build → restart → `/health` (rollback bila gagal) → migration → pasang cron agregat.
- **Traefik**: `/docker/traefik-gexn/api-router.yml` di-mount sebagai file tunggal — edit *in-place*. Router `api-kopicalf` → `http://127.0.0.1:8002`.
- **Log**: `docker logs integrated-portal-be`, `/var/log/integrated-portal-be-deploy.log`, `/var/log/integrated-portal-aggregates.log`, `/var/log/integrated-portal-cost-control.log`.
- **Filter cabang**: semua endpoint menerima `branch=CCI01,CCI04` (dinormalisasi: trim, unik, urut; SQL `branch_code = ANY(...)`).
- **Cek realtime**: `curl https://api.kopicalf.co.id/api/realtime/version`; handshake WebSocket harus `101` untuk origin yang diizinkan.

## 8. Login, akun & role

`app/routes/auth.py`, `app/routes/users.py`, `app/routes/avatars.py`, `app/accounts.py`, `app/security.py` — `app/profile.py` — migration `004_user_account.sql`, `005_user_avatar.sql`, `006_user_profile.sql`.

Superadmin cukup membuat **login** (username, email, password, role); user melengkapi identitasnya sendiri di *My profile* (`PATCH /api/auth/me`).

**Tabel `integration_portal.user_account`**

| Kolom | Keterangan |
|---|---|
| `id` | uuid |
| `username` | unik (tidak peka huruf besar/kecil), 3–32 karakter `a-z 0-9 . _ -`, disimpan huruf kecil |
| `email` | unik (tidak peka huruf besar/kecil), disimpan huruf kecil |
| `full_name` | nama lengkap (opsional sampai diisi user; selama kosong UI menampilkan username — `displayName`) |
| `password_hash` | scrypt (`scrypt$N$r$p$salt$hash`); password tidak pernah disimpan/dikembalikan |
| `role` | `superadmin` (akses penuh: platform & user accounts) / `user` (hanya dashboard Overview & Sales) |
| `is_active` | user nonaktif tidak bisa login; menonaktifkan langsung mencabut semua sesinya |
| `phone_number`, `job_title`, `department` | profil; bersama `full_name` menentukan `profileComplete` |
| `employee_number`, `gender` (`male`/`female`), `birth_date`, `address`, `city` | profil (opsional) |
| `work_branch_code` | lokasi kerja = `integration_esb.master_branches.branch_code` (API juga mengembalikan `workBranchName`) |
| `profile_updated_at` | terakhir user mengubah profilnya sendiri |
| `notes` | catatan admin (hanya superadmin) |
| `must_change_password` | wajib ganti password setelah login berikutnya |
| `last_login_at`, `last_login_ip` | login terakhir |
| `failed_login_attempts`, `locked_until` | 5 kali salah berturut-turut → terkunci 15 menit |
| `password_changed_at`, `avatar_updated_at` | audit; versi URL foto |
| `created_at`, `created_by`, `updated_at`, `updated_by` | audit (by = user yang mengubah) |

**Keamanan**

- Login dengan **username** atau **email** (`method`), pesan galat sama untuk user tidak ada / password salah, dan waktu respons sama (tidak bisa menebak akun yang terdaftar).
- Sesi: token acak 256-bit di cookie `portal_session` (HttpOnly, Secure, SameSite=Lax, 12 jam atau 30 hari bila "keep me signed in"); database hanya menyimpan SHA-256 token. Cookie berlaku di `portal.kopicalf.co.id` karena browser memanggil API lewat proxy Next.js (same-origin).
- Ganti password sendiri mencabut sesi di perangkat lain; reset password, ganti role atau menonaktifkan user mencabut semua sesi user tersebut. Hasil pengecekan sesi di-cache 20 detik per worker.
- *My profile* (`PATCH /api/auth/me`) hanya menerima field profil (`fullName`, `phoneNumber`, `jobTitle`, `department`, `employeeNumber`, `gender`, `birthDate`, `address`, `city`, `workBranchCode`); field lain (username, email, role, status, notes) ditolak 422. Validasi sama dengan form superadmin (`app/profile.py`): telepon `0-9 + ( ) -`, nomor karyawan `A-Z 0-9 . / -`, tanggal lahir 1900…hari ini, lokasi kerja harus ada di master branch.
- Pengaman: tidak bisa menghapus/menonaktifkan akun sendiri; superadmin aktif terakhir tidak bisa dihapus, dinonaktifkan atau diturunkan rolenya.
- Foto profil: dikirim sebagai data URL yang sudah dipotong & diperkecil di browser (256×256); server memeriksa tipe (webp/jpeg/png), ukuran (≤ 512 KB) dan *file signature*.

**CLI** (di VPS: `docker exec integrated-portal-be python -m app.accounts …`)

```bash
python -m app.accounts create --username budi --email budi@kopicalf.co.id            # --full-name opsional, --role user|superadmin
python -m app.accounts reset-password --username superadmin     # password baru + buka kunci
```

Password dibuat acak dan ditampilkan sekali (atau diambil dari env `PORTAL_NEW_PASSWORD`); akun ditandai wajib ganti password saat login pertama.
