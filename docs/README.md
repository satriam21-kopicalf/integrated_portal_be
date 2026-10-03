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
| `schema_migrations` | migration yang sudah dijalankan (`app/migrations/*.sql`, otomatis setelah deploy) |

Agregat dibangun ulang per tanggal oleh `app/aggregates.py` dan direkonsiliasi dengan data mentah. Cron VPS `/etc/cron.d/integrated-portal-aggregates`: tiap jam menit :20 (hari ini + kemarin) dan 02:50 WIB (8 hari).

## 4. Endpoint

| Endpoint | Keterangan |
|---|---|
| `GET /health` | status + koneksi DB |
| `GET /api/transactions`, `/api/transactions/{sales_num}` | daftar (cursor pagination, rentang bebas) dan detail |
| `GET /api/summary` | Gross − Void − Other Cost − Open = Sales per hari & total. **Hari sebelum kemarin dibaca dari `agg_sales_daily`** (hasil identik, 1 tahun ±0,5 s; sebelumnya 74 s); hari ini/kemarin dan hari yang belum diagregasi dibaca mentah |
| `GET /api/branches` | master cabang + jumlah transaksi 65 hari |
| `GET /api/overview/*` | 11 endpoint Overview (meta, kpis, trend, channels, branches, hourly, menus, deductions, monthly, payments, basket); filter `dateFrom`, `dateTo`, `branch`, `channel`. `monthly` menampilkan bulan-bulan di dalam periode terpilih |
| `GET /api/live` | hari ini: total, per jam, per channel, vs kemarin di jam yang sama (sales, bills, nett), total & per jam kemarin, batch sinkron terakhir, transaksi terbaru (`limit`, `branch`, `channel`) |
| `POST /api/exports`, `GET /api/exports/{id}`, `/download` | export Excel (proses terpisah per job, maks. `EXPORT_MAX_CONCURRENT`) |
| `WS /ws`, `GET /api/realtime/version` | realtime (lihat §5) |

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

## 7. Operasional

- **Deploy**: push ke `main` → VPS (`scripts/auto-deploy.sh`, cron 5 menit): test → build → restart → `/health` (rollback bila gagal) → migration → pasang cron agregat.
- **Traefik**: `/docker/traefik-gexn/api-router.yml` di-mount sebagai file tunggal — edit *in-place*. Router `api-kopicalf` → `http://127.0.0.1:8002`.
- **Log**: `docker logs integrated-portal-be`, `/var/log/integrated-portal-be-deploy.log`, `/var/log/integrated-portal-aggregates.log`.
- **Cek realtime**: `curl https://api.kopicalf.co.id/api/realtime/version`; handshake WebSocket harus `101` untuk origin yang diizinkan.
