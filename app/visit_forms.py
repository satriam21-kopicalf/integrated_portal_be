"""Form → Monitoring Product / Monitoring Outlet and their Monitoring pages (2026-10-20), from the workbook
"Form Visit" (integrated_portal docs): Monitoring Product = AVAILIBILITY to KUALITAS PRODUK, Monitoring Outlet =
KEBERSIHAN DAN RATING OUTLET to KENYAMANAN OUTLET, plus ADMINISTRASI & FINANCE (added 2026-10-21); the overall
performance and the signatures of the workbook are not part of these forms.

A PIC visits one outlet at a time: the visit is a draft (saved as it is filled, continued later) for the signed-in
account and the branch picked (a PIC with several branches fills one visit per branch), then submitted. Every item is
rated UNDER / MEET / EXCEED with a note and camera photos (stamped in the browser with the time, the branch and the
position; the server keeps the position and its distance to the outlet). Every rated item needs a note and at least one
photo — material for the analysis (2026-10-23; before: UNDER only); items left unrated do not block the sending.
Daily (2026-10-23): one visit per PIC, form and branch per day (WIB) — the next one the day after; a day without any
visit of a PIC counts as a day off in the monitoring (attendance).
Tables integration_portal.visit_form and visit_form_photo (migration 023).
Form WW-WR (2026-11-03, migration 064, app/wwwr.py): the SWAT audit of an outlet, every item of both forms in one
visit (keys "product:…" / "outlet:…"), filled by the members of a scheduled audit and sent by its lead.
"""
import json
from datetime import date, datetime, timedelta
from typing import Any, Optional

from app import database as db
from app.pic_checkin import CheckInError, branch_options, judge, locations, parse_photo

T = "integration_portal.visit_form"
TP = "integration_portal.visit_form_photo"
RATINGS = ("UNDER", "MEET", "EXCEED")
MAX_PHOTOS_PER_ITEM = 3
DAILY_SINCE = date(2026, 10, 23)  # one visit per branch per day from here; days off are counted from this day


class VisitError(CheckInError):
    """A visit refused with a message for the PIC (HTTP 422)."""


REQ, OPT = "required", "optional"


def _it(prefix: str, no: int, label: str, label_en: str, *, kind: str = "rating", required: bool = True,
        required_for: Optional[list[str]] = None, only_for: Optional[list[str]] = None, note: Optional[str] = None,
        photo: Optional[str] = None, **extra) -> dict:
    """One item. required: it must be rated before sending (required_for: only at the outlets of these branch groups,
    Monitoring → Branch groups). only_for: the item is asked only at the outlets of these groups (2026-11-02; the
    others do not see it and it is not assessed there). note / photo: what a rated required item needs (both by
    default); an optional item needs neither."""
    item = {"key": f"{prefix}.{no}", "no": no, "label": label, "labelEn": label_en, "kind": kind, "required": required,
            "note": note or REQ, "photo": photo or REQ, **extra}
    if required_for:
        item["requiredFor"] = required_for
    if only_for:
        item["onlyFor"] = only_for
    return item


def _list(prefix: str, rows: list[tuple]) -> list[dict]:
    """[(label, label_en, options?), ...] numbered from 1."""
    return [_it(prefix, i, r[0], r[1], **(r[2] if len(r) > 2 else {})) for i, r in enumerate(rows, start=1)]


def _sec(key: str, title: str, title_en: str, items: list[dict], retired: bool = False) -> dict:
    """retired: a list no longer asked (the visits sent with it still show it)."""
    return {"key": key, "title": title, "titleEn": title_en, "items": items, **({"retired": True} if retired else {})}


def _grp(key: str, title: str, title_en: str, subtitle: str, subtitle_en: str, sections: list[dict]) -> dict:
    return {"key": key, "title": title, "titleEn": title_en, "subtitle": subtitle, "subtitleEn": subtitle_en, "sections": sections}


O = {"required": False}  # optional item

# Stock preparation and shelf-life labels (2026-10-27, Operations): the premixes and liquids below are required at every
# outlet (CTG, CTGP, CCI); the rest optional.
PREMIX = [
    ("Premix Premium", "Premix Premium", {}), ("Premix Regular", "Premix Regular", {}),
    ("Premix Premium Oat", "Premix Premium Oat", {}), ("Premix Smooth", "Premix Smooth", {}),
    ("Premix Magic", "Premix Magic", {}), ("Premix Jefferson", "Premix Jefferson", O),
    ("Premix Coklat", "Premix Chocolate", {}), ("Premix Tea Series", "Premix Tea Series", {}),
    ("Premix Matcha", "Premix Matcha", {}), ("Premix Taro", "Premix Taro", O),
    ("Premix Dirty & Ice White", "Premix Dirty & Ice White", O), ("Cold Brew Liquid", "Cold Brew Liquid", {}),
    ("Cream Base", "Cream Base", {}), ("Creamer Liquid", "Creamer Liquid", {}),
    ("Dimsum", "Dim Sum", O), ("Roti Kukus", "Steamed Bun", O), ("Puding", "Pudding", O),
    ("Side Dish Lainnya", "Other Side Dishes", O), ("Maincourse", "Main Course", O),
]
EVIDENCE_NOTE_ONLY = {"note": REQ, "photo": OPT}  # a note is required, the photo is not
# Stock Preparation, Espresso and Beans (2026-10-28, Operations): the PIC also types the stock in grams (net weight); a
# rated required item needs it, like its note and photo
QTY = {"qty": "g"}
MAX_QTY_G = 1_000_000

CATEGORY_EN = {"Kritikal": "Critical", "Mayor": "Major", "Minor": "Minor"}


def _tp(label: str, label_en: str, category: str, detail: str, detail_en: str, standard: str, standard_en: str,
        required: bool = True) -> tuple:
    """A Travel Path point (SOP Rev. 3): one photo, the note optional; the photo points and the standard in ID / EN."""
    return (label, label_en, {"detail": detail, "detailEn": detail_en, "standard": standard, "standardEn": standard_en,
                              "category": category, "categoryEn": CATEGORY_EN[category], "note": OPT, "photo": REQ,
                              **({} if required else {"required": False})})


DEFINITIONS: dict[str, dict] = {
    "product": {
        "title": "Monitoring Product",
        "titleEn": "Monitoring Product",
        "groups": [
            _grp("availability", "Ketersediaan", "Availability", "Ketersediaan dan Kesesuaian Produk", "Product Availability and Conformity", [
                _sec("stok-preparation", "Stock Preparation", "Stock Preparation",
                     _list("stok-preparation", [(f"Ketersediaan Stok {i}", f"{e} Stock Available", {**o, **QTY}) for i, e, o in PREMIX])),
                _sec("shelf-life", "Label Shelf Life", "Shelf Life Label",
                     _list("shelf-life", [(f"Label Shelf Life {i}", f"{e} Shelf Life Label", o) for i, e, o in PREMIX])),
                # Beans Specialty: required at the CTGP and CCI outlets only (2026-10-27); the items marked only_for are not
                # asked at the CTG outlets (Store Performance workbook, sheet reff.: ctg 0 — 2026-11-02)
                _sec("espresso-beans", "Espresso dan Beans", "Espresso and Beans", _list("espresso-beans", [
                    ("Ketersediaan Stok Espresso Premium", "Espresso Premium Stock Available", QTY),
                    ("Ketersediaan Stok Espresso Regular", "Espresso Regular Stock Available", QTY),
                    ("Ketersediaan Stok Beans Premium", "Beans Premium Stock Available", {**O, **QTY, "only_for": ["CTGP", "CCI"]}),
                    ("Ketersediaan Stok Beans Regular", "Beans Regular Stock Available", {**O, **QTY, "only_for": ["CTGP", "CCI"]}),
                    ("Ketersediaan Stok Beans Cold Brew", "Cold Brew Beans Stock Available", QTY),
                    ("Ketersediaan Stok Beans Specialty", "Specialty Beans Stock Available",
                     {"required": False, "required_for": ["CTGP", "CCI"], "only_for": ["CTGP", "CCI"], **QTY}),
                ])),
                _sec("bahan-penolong", "Bahan Penolong", "Supporting Materials", _list("bahan-penolong", [
                    ("Ketersediaan Cup Es", "Iced Cups Available"), ("Ketersediaan Cup Frosted", "Frosted Cups Available"),
                    ("Ketersediaan Cup Jefferson", "Jefferson Cups Available", {**O, "only_for": ["CTGP", "CCI"]}), ("Ketersediaan Paper Cup", "Paper Cups Available"),
                    ("Ketersediaan Es Batu", "Ice Cubes Available"), ("Ketersediaan Es Batu dalam Klip", "Ice Cubes in Clip Bags Available"),
                    ("Ketersediaan Can dan Tutup Can (Sudah Stiker)", "Cans and Can Lids Available (Stickered)"),
                    ("Ketersediaan Botol dan Tutup Botol (Sudah Stiker)", "Bottles and Bottle Caps Available (Stickered)"),
                    ("Ketersediaan Straw 5 mm", "5 mm Straws Available"), ("Ketersediaan Straw 6 mm", "6 mm Straws Available", {**O, "only_for": ["CTGP", "CCI"]}),
                    ("Ketersediaan Lunch Box S", "Lunch Boxes S Available", O), ("Ketersediaan Lunch Box M", "Lunch Boxes M Available", O),
                    ("Ketersediaan Set Spork", "Spork Sets Available", O), ("Ketersediaan Greeting Card", "Greeting Cards Available"),
                    ("Ketersediaan Beans Card Specialty", "Specialty Beans Cards Available", O),
                    ("Ketersediaan Plastik Take Away", "Take-Away Bags Available"),
                ])),
                _sec("bahan-baku", "Bahan Baku", "Raw Materials", _list("bahan-baku", [
                    ("Kesesuaian Form Request Bahan Baku", "Raw Material Request Form Matches"),
                    ("Kesesuaian Form Request FM, Oat Milk, Ice Cream, Brookfarm", "FM, Oat Milk, Ice Cream, Brookfarm Request Form Matches"),
                    ("Kesesuaian Form Request Ice Hot, Rich Gold", "Ice Hot, Rich Gold Request Form Matches", {**O, "only_for": ["CTGP", "CCI"]}),
                    ("Kesesuaian Form Request Bahan Baku Main Course", "Main Course Raw Material Request Form Matches", {**O, "only_for": ["CTGP", "CCI"]}),
                    ("Kesesuaian Form Request Bahan Baku Side Dish", "Side Dish Raw Material Request Form Matches"),
                ])),
            ]),
            _grp("kualitas-produk", "Kualitas Produk", "Product Quality", "5 Indra - Rasa", "5 Senses - Taste", [
                _sec("kualitas-produk", "Kualitas Produk", "Product Quality", _list("kualitas-produk", [
                    ("Cupping Premium", "Cupping Premium"), ("Cupping Regular", "Cupping Regular"),
                    ("Cupping Semua Premix", "Cupping All Premixes"), ("Cupping Semua Cream & Foam", "Cupping All Cream & Foam"),
                    # optional (2026-11-02): not every outlet sells the specialty menu
                    ("Cupping Menu Specialty", "Cupping Specialty Menu", O),
                    ("Test Food WIP Side Dish & Main Course", "Test Food WIP Side Dish & Main Course", O),
                    ("Test Food Menu Side Dish", "Test Food Side Dish Menu", O),
                    ("Test Food Menu Main Course", "Test Food Main Course Menu", O),
                    ("Handling & Treatment Produk", "Product Handling & Treatment"),
                ])),
            ]),
        ],
    },
    "outlet": {
        "title": "Monitoring Outlet",
        "titleEn": "Monitoring Outlet",
        "groups": [
            _grp("kebersihan-rating", "Kebersihan dan Rating Outlet", "Outlet Cleanliness and Rating", "5 Indra - Penglihatan", "5 Senses - Sight", [
                # Outlet Cleanliness = Travel Path SOP Rev. 3 (2026-10-30), one photo per point; Ambience, Grooming & Kesiapan
                # Crew and Cupping & Test Food Daily removed (2026-11-02)
                _sec('tp-eksterior', 'Zona 1 — Eksterior & Pintu Masuk', 'Zone 1 — Exterior & Entrance', _list('tp-eksterior', [
                    _tp('Signage, Fasad & Pintu Masuk', 'Signage, Facade & Entrance', 'Minor',
                        'Signage outlet · pintu dan kaca depan · papan promo dan jam operasional',
                        'Outlet signage · front door and glass · promo board and opening hours',
                        'Signage menyala dan bersih, kaca bebas bekas tangan, promo yang terpasang masih berjalan, jam operasional terbaca dari luar.',
                        'Signage lit and clean, glass free of hand marks, the promos shown still running, opening hours readable from outside.'),
                    _tp('Area Outdoor & Sekitar Outlet', 'Outdoor Area & Surroundings', 'Minor',
                        'Meja dan kursi outdoor · trotoar dan area parkir · titik sampah di luar',
                        'Outdoor tables and chairs · sidewalk and parking · outside bins',
                        'Tidak ada sampah berserakan atau puntung, furniture pada posisi layout, area sekitar outlet rapi dan akses masuk tidak terhalang.',
                        'No litter or cigarette butts, furniture in its layout position, the surroundings tidy and the entrance not blocked.', required=False),
                    _tp('Tanaman', 'Plants', 'Mayor', 'Tanaman', 'Plants',
                        'Menyiram tanaman dan memastikan tanaman tidak mati.', 'Plants watered and none of them dead.', required=False),
                ])),
                _sec('tp-customer', 'Zona 2 — Area Customer', 'Zone 2 — Customer Area', _list('tp-customer', [
                    _tp('Lantai, Meja & Kursi', 'Floor, Tables & Chairs', 'Mayor',
                        'Lantai area customer · meja dan kursi · dinding, kaca, pencahayaan',
                        'Customer area floor · tables and chairs · walls, glass, lighting',
                        'Lantai bersih dan kering tanpa tumpahan, meja sudah di-clear dan dilap, kursi sesuai layout, semua lampu menyala.',
                        'Floor clean and dry with no spills, tables cleared and wiped, chairs in layout, every light on.'),
                    _tp('Toilet / Restroom', 'Toilet / Restroom', 'Mayor',
                        'Lantai & kloset · wastafel & cermin · sabun, tisu, tempat sampah · kartu kontrol kebersihan',
                        'Floor & toilet bowl · sink & mirror · soap, tissue, bin · cleaning control card',
                        'Bersih, kering, tidak berbau. Sabun dan tisu tersedia, tempat sampah tidak penuh, kartu kontrol terisi sesuai jam pemeriksaan.',
                        'Clean, dry, no odour. Soap and tissue available, the bin not full, the control card filled at the check times.', required=False),
                    _tp('Display Produk & Merchandise', 'Product & Merchandise Display', 'Kritikal',
                        'Showcase pastry atau produk siap jual · label tanggal / best before · display merchandise dan harga',
                        'Pastry or ready-to-sell showcase · date / best-before labels · merchandise display and prices',
                        'Tidak ada produk lewat masa simpan di display, semua produk berlabel tanggal, showcase pada suhu standar, harga terbaca dan sesuai POS.',
                        'No product past its shelf life on display, every product date-labelled, showcase at standard temperature, prices readable and matching the POS.', required=False),
                ])),
                _sec('tp-counter', 'Zona 3 — Counter & Bar', 'Zone 3 — Counter & Bar', _list('tp-counter', [
                    _tp('Kasir, Antrean & Menu Board', 'Cashier, Queue & Menu Board', 'Mayor',
                        'Area kasir dan jalur antrean · menu board atau digital menu · daftar item habis',
                        'Cashier area and queue line · menu board or digital menu · sold-out list',
                        'Meja kasir rapi tanpa barang pribadi, POS dan printer berfungsi, menu board bersih dan harga sesuai sistem, item habis sudah ditandai dan diinformasikan ke crew.',
                        'Cashier desk tidy with no personal items, POS and printer working, menu board clean and prices matching the system, sold-out items marked and told to the crew.'),
                    _tp('Bar Counter & Station', 'Bar Counter & Station', 'Mayor',
                        'Bar counter tampak depan · bar mat dan area bawah counter · rak cup, lid, sedotan',
                        'Bar counter from the front · bar mat and under the counter · cup, lid and straw racks',
                        'Counter bersih dari tumpahan, tidak ada alat kotor menumpuk, packaging tersimpan tertutup dan tidak menyentuh lantai, station siap sesuai par level.',
                        'Counter free of spills, no dirty tools piling up, packaging stored closed and off the floor, station ready to par level.'),
                    # optional (2026-11-02)
                    _tp('Mesin Espresso, Grinder & Kalibrasi', 'Espresso Machine, Grinder & Calibration', 'Kritikal',
                        'Group head dan steam wand · hopper dan grinder · catatan kalibrasi shift (dose, yield, waktu)',
                        'Group head and steam wand · hopper and grinder · shift calibration notes (dose, yield, time)',
                        'Group head sudah di-backflush, steam wand bebas residu susu dan di-purge, hopper bersih, hasil kalibrasi dalam rentang resep dan tercatat, susu steam 60–66 °C.',
                        'Group head backflushed, steam wand purged and free of milk residue, hopper clean, calibration within the recipe range and recorded, steamed milk 60–66 °C.', required=False),
                    _tp('Tools: Sealer, Can Sealer, Blender', 'Tools: Sealer, Can Sealer, Blender', 'Mayor',
                        'Foto close-up tiap alat dalam kondisi siap pakai', 'Close-up photo of each tool ready to use',
                        'Alat bersih dari residu produk, tidak berkarat atau rusak, kabel aman, hasil seal rapat pada uji pertama.',
                        'Tools free of product residue, no rust or damage, cables safe, the seal tight on the first test.'),
                    _tp('Hand Sink & Area Pencucian Alat', 'Hand Sink & Tool Washing Area', 'Kritikal',
                        'Hand sink crew · area cuci alat · rak pengeringan', 'Crew hand sink · tool washing area · drying rack',
                        'Air mengalir, sabun dan pengering tangan tersedia. Hand sink tidak dipakai mencuci alat atau menumpuk barang. Alat bersih disimpan terbalik dan kering.',
                        'Running water, soap and hand dryer available. The hand sink is not used to wash tools or store items. Clean tools stored upside down and dry.'),
                    _tp('Ice Cube & Ice Bin', 'Ice Cubes & Ice Bin', 'Kritikal',
                        'Ice cube dalam plastik klip di penyimpanan · label tanggal produksi · ice bin dan scoop',
                        'Ice cubes in clip bags in storage · production date label · ice bin and scoop',
                        'Plastik klip tertutup rapat dan tidak bocor, berlabel tanggal dan dipakai FIFO, ice bin bersih, scoop disimpan di luar es dengan pegangan tidak menyentuh es.',
                        'Clip bags sealed and not leaking, date-labelled and used FIFO, ice bin clean, scoop kept out of the ice with the handle not touching it.'),
                    _tp('Waste Bin Area Bar', 'Bar Area Waste Bin', 'Minor',
                        'Waste bin di area bar dan kasir', 'Waste bins at the bar and the cashier',
                        'Tertutup, tidak penuh, kantong tidak bocor, tidak ada cairan menggenang di dasar bin.',
                        'Closed, not full, bag not leaking, no liquid pooling at the bottom.'),
                ])),
                _sec('tp-boh', 'Zona 4 — Back of House', 'Zone 4 — Back of House', _list('tp-boh', [
                    _tp('Chiller & Freezer', 'Chiller & Freezer', 'Kritikal',
                        'Tampak luar dan dalam · seal pintu · display suhu · susunan bahan di dalam',
                        'Outside and inside · door seal · temperature display · how the stock is arranged',
                        'Chiller 1–4 °C dan freezer −18 °C atau lebih rendah, angka terbaca di foto. Seal rapat, tidak ada bunga es berlebih atau kebocoran, bahan tertutup dan berlabel, produk siap saji tidak di bawah bahan mentah.',
                        'Chiller 1–4 °C and freezer −18 °C or lower, readable in the photo. Seal tight, no excess frost or leaks, stock covered and labelled, ready-to-serve products not below raw materials.'),
                    _tp('Storage, FIFO & Label', 'Storage, FIFO & Labels', 'Kritikal',
                        'Rak dry storage · kemasan bahan baku dan labelnya · lantai dan dinding storage',
                        'Dry storage racks · raw material packaging and labels · storage floor and walls',
                        'Tersusun FIFO dan berlabel tanggal terima, tidak ada kemasan rusak atau kedaluwarsa, bahan minimal 15 cm di atas lantai dan tidak menempel dinding, area kering dan bebas hama.',
                        'Arranged FIFO and labelled with the receiving date, no damaged or expired packaging, stock at least 15 cm off the floor and away from the wall, area dry and pest-free.'),
                    _tp('Chemical & Sanitasi', 'Chemicals & Sanitation', 'Kritikal',
                        'Rak penyimpanan chemical · botol sanitizer dan kain lap · test strip',
                        'Chemical storage rack · sanitizer bottles and cloths · test strips',
                        'Chemical disimpan terpisah dan tidak di atas bahan pangan, semua botol berlabel dan tidak dipindah ke wadah minuman, sanitizer pada konsentrasi standar dan diganti sesuai jadwal, kain lap bersih.',
                        'Chemicals stored apart and not above food, every bottle labelled and never moved into drink containers, sanitizer at standard strength and changed on schedule, cloths clean.'),
                    _tp('Area Sampah Akhir & Drainase', 'Final Waste Area & Drainage', 'Mayor',
                        'Tempat pembuangan akhir di luar outlet · grease trap · floor drain',
                        'Final waste point outside the outlet · grease trap · floor drain',
                        'Sampah tertutup rapat dan tidak menumpuk, tidak ada bau menyengat atau lalat, saluran air lancar, grease trap dibersihkan sesuai jadwal.',
                        'Waste closed and not piling up, no strong smell or flies, drains flowing, grease trap cleaned on schedule.', required=False),
                    _tp('Pest Control', 'Pest Control', 'Kritikal',
                        'Titik rawan: sudut storage, bawah sink, celah pintu · perangkap dan kartu kunjungan pest control',
                        'Risk points: storage corners, under the sink, door gaps · traps and the pest control visit card',
                        'Tidak ada jejak hama (kotoran, gigitan kemasan, serangga), celah pintu dan ventilasi tertutup, perangkap terpasang di titik standar, kunjungan terakhir tercatat dan belum lewat jadwal.',
                        'No sign of pests (droppings, chewed packaging, insects), door gaps and vents closed, traps at the standard points, the last visit recorded and not overdue.'),
                    _tp('Keselamatan & Kelistrikan', 'Safety & Electrical', 'Kritikal',
                        'APAR dan tanggal inspeksinya · panel listrik dan kabel area bar · jalur keluar · kotak P3K',
                        'Fire extinguisher and its inspection date · electrical panel and bar cables · exit route · first-aid kit',
                        'APAR terpasang, tekanan pada zona hijau, masa inspeksi berlaku. Tidak ada kabel terbuka atau sambungan basah. Jalur keluar bebas hambatan, isi P3K lengkap.',
                        'Extinguisher in place, pressure in the green zone, inspection valid. No exposed cables or wet connections. Exit route clear, first-aid kit complete.'),
                ])),
                _sec('tp-crew', 'Zona 5 — Crew & Dokumen', 'Zone 5 — Crew & Documents', _list('tp-crew', [
                    _tp('Area Staff & Loker', 'Staff Area & Lockers', 'Minor',
                        'Area loker dan tempat istirahat staf', 'Locker area and staff rest area',
                        'Rapi, barang pribadi tidak bercampur dengan bahan baku atau alat produksi, tidak ada makanan atau minuman pribadi di area produksi.',
                        'Tidy, personal items kept apart from raw materials and tools, no personal food or drinks in the production area.', required=False),
                    _tp('Log & Dokumen Outlet', 'Outlet Logs & Documents', 'Mayor',
                        'Log suhu harian · checklist cleaning · kartu kontrol toilet · catatan kalibrasi termometer',
                        'Daily temperature log · cleaning checklist · toilet control card · thermometer calibration record',
                        'Semua log terisi pada jam yang seharusnya dan ditandatangani, tidak ada baris kosong atau diisi belakangan, termometer dikalibrasi sesuai jadwal.',
                        'Every log filled at its time and signed, no blank or back-filled rows, thermometer calibrated on schedule.'),
                ])),
                # platform = the stars of the outlet on that platform go with the item (2026-10-21): Google from
                # Aggregator → Google Maps (automatic), the delivery apps typed by the PIC
                _sec("rating", "Rating Outlet", "Outlet Rating", _list("rating", [
                    ("Rating Google", "Google Rating", {"platform": "google"}),
                    ("Rating GoFood", "GoFood Rating", {"platform": "gofood"}),
                    ("Rating GrabFood", "GrabFood Rating", {"platform": "grabfood"}),
                    ("Rating ShopeeFood", "ShopeeFood Rating", {"platform": "shopeefood"}),
                    ("Komplain Customer (7 Hari Terakhir)", "Customer Complaints (Last 7 Days)"),
                ])),
            ]),
            _grp("barista-flow", "Barista + Flow Bar", "Barista + Bar Flow", "Memastikan Peran dan Tanggung Jawab di Bar",
                 "Roles and Responsibilities in the Bar", [
                _sec("barista", "Barista", "Barista", _list("barista", [
                    ("Grooming Barista", "Barista Grooming"),
                    ("Kedisiplinan Barista", "Barista Discipline", O),
                    ("Nama Barista Telat", "Names of Late Baristas", {"kind": "text", "required": False}),
                    ("Update Rekap Absen Barista", "Barista Attendance Recap Updated"),
                ])),
                # Speed of service and roles: a note is required, no photo needed; the bar flow: note and photo
                _sec("flow-bar", "Flow Bar", "Bar Flow", _list("flow-bar", [
                    ("Speed of Service", "Speed of Service", EVIDENCE_NOTE_ONLY),
                    ("Kesesuaian Role Barista", "Barista Roles Followed", EVIDENCE_NOTE_ONLY),
                    ("Kesesuaian Flow Bar", "Bar Flow Followed"),
                ])),
            ]),
            _grp("hospitality", "Hospitality", "Hospitality", "5 Indra - Pendengaran - Product Knowledge", "5 Senses - Hearing - Product Knowledge", [
                _sec("hospitality", "Hospitality", "Hospitality", _list("hospitality", [
                    ("Product Knowledge Barista", "Barista Product Knowledge", EVIDENCE_NOTE_ONLY),
                    ("Greetings Barista", "Barista Greetings", EVIDENCE_NOTE_ONLY),
                    ("Offering Product Barista", "Barista Product Offering", EVIDENCE_NOTE_ONLY),
                    ("Musik", "Music", EVIDENCE_NOTE_ONLY),
                ])),
            ]),
            _grp("smell", "Aroma", "Smell", "5 Indra - Wangi Outlet - Alat Kebersihan", "5 Senses - Outlet Scent - Cleaning Tools", [
                _sec("alat-kebersihan", "Kelengkapan Alat Kebersihan", "Cleaning Tools Complete", _list("alat-kebersihan", [
                    ("Sapu", "Broom"), ("Pengki", "Dustpan"), ("Pel Area Bar", "Bar Area Mop"), ("Pel Area Customer", "Customer Area Mop"),
                    ("Wiper", "Squeegee"), ("Pel Karet", "Rubber Mop"), ("Sikat Mesin Kopi & Grinder", "Coffee Machine & Grinder Brush", O),
                    ("Lap Area Bar Depan", "Front Bar Cloth"), ("Lap Area Kitchen & Bar Belakang", "Kitchen & Back Bar Cloth"),
                    ("Lap Area Customer", "Customer Area Cloth"), ("Lap Gelas", "Glass Cloth", O), ("Lap Tools", "Tools Cloth"),
                    ("Lap Mesin Kopi", "Coffee Machine Cloth", O), ("Lap Steam Milk", "Steam Wand Cloth", O),
                    ("Lap Basket Porta", "Portafilter Basket Cloth", O),
                ])),
                _sec("chemical", "Kelengkapan Chemical", "Chemicals Complete", _list("chemical", [
                    ("Sabun Kaca", "Glass Cleaner"), ("Sabun Cuci Piring", "Dish Soap"), ("Sabun Cuci Tangan", "Hand Soap"),
                    ("Sabun Lantai", "Floor Cleaner"), ("Sabun Toilet", "Toilet Cleaner", O), ("Pengharum AC", "AC Freshener"),
                    ("Pengharum Ruangan", "Room Freshener"), ("Pengharum Musala & Toilet", "Prayer Room & Toilet Freshener", O),
                ])),
            ]),
            _grp("kenyamanan", "Kenyamanan Outlet", "Outlet Comfort", "5 Indra - Suhu Ruangan & Kenyamanan Outlet",
                 "5 Senses - Room Temperature & Outlet Comfort", [
                _sec("kenyamanan", "Kenyamanan Outlet", "Outlet Comfort", _list("kenyamanan", [
                    ("Kondisi Ruangan", "Room Condition"),
                    ("Kondisi Furniture (Sofa, Meja, Karpet, Kursi)", "Furniture Condition (Sofa, Table, Carpet, Chair)"),
                    ("Kondisi Internet Customer", "Customer Internet"), ("Kondisi Merchandise", "Merchandise Condition", O),
                ])),
            ]),
            # added 2026-10-21 (user request): the workbook's ADMINISTRASI & FINANCE; SO Packaging became SO Daily (2026-10-27)
            _grp("administrasi-finance", "Administrasi & Finance", "Administration & Finance", "Administrasi & Finance Outlet",
                 "Outlet Administration & Finance", [
                _sec("administrasi-finance", "Administrasi & Finance", "Administration & Finance", _list("administrasi-finance", [
                    ("SO Daily", "Daily Stock Opname"), ("SO Weekly / Monthly", "Weekly / Monthly Stock Opname"),
                    ("Daily Cupping", "Daily Cupping"), ("Dokumentasi Surat Jalan", "Delivery Note Documentation"),
                    ("Form Self Pickup", "Self Pickup Form", O), ("Penggunaan Petty Cash Terkontrol", "Petty Cash Use Under Control"),
                    ("Setoran Uang Pembelian Menggunakan Cash", "Cash Purchase Money Deposited"),
                ])),
            ]),
        ],
    },
}


PARTS = {"product": "Product", "outlet": "Outlet"}


def _combined() -> dict:
    """WW-WR: the groups of Monitoring Product then Monitoring Outlet, every key prefixed with its form ("product:…")."""
    groups = []
    for form, label in PARTS.items():
        for g in DEFINITIONS[form]["groups"]:
            groups.append({**g, "key": f"{form}:{g['key']}", "part": form, "title": f"{label} · {g['title']}",
                           "titleEn": f"{label} · {g.get('titleEn', g['title'])}",
                           "sections": [{**sec, "key": f"{form}:{sec['key']}",
                                         "items": [{**it, "key": f"{form}:{it['key']}"} for it in sec["items"]]}
                                        for sec in g["sections"]]})
    return {"title": "WW-WR", "titleEn": "WW-WR", "groups": groups}


DEFINITIONS["wwwr"] = _combined()
AUDIT = "integration_portal.wwwr_audit"
AUDIT_MEMBER = "integration_portal.wwwr_audit_member"


def audit_of(visit_id: str) -> Optional[dict]:
    """The WW-WR audit of a visit: its lead and members."""
    return db.fetchrow(f"""SELECT a.id::text, a.lead_id::text, a.status, a.day,
                                  array(SELECT m.user_id::text FROM {AUDIT_MEMBER} m WHERE m.audit_id = a.id) AS members
                             FROM {AUDIT} a WHERE a.visit_id::text = %s""", (visit_id,))


def can_fill(row: dict, user: dict) -> bool:
    """The PIC's own visit; a WW-WR visit: a member of its audit (not cancelled)."""
    if row["form"] != "wwwr":
        return row["user_id"] == str(user["id"])
    a = audit_of(row["id"])
    return bool(a and a["status"] != "cancelled" and str(user["id"]) in (a["members"] or []))


def definition(form: str) -> dict:
    """The definition of a form with the items each branch group is asked after Monitoring OPS → Branch groups
    (app/form_scope.py, 2026-11-04): what the screens and the scores use."""
    from app import form_scope
    return form_scope.apply(form, DEFINITIONS[form])


def all_items(form: str, retired: bool = False) -> list[dict]:
    """Every item of a form with its group and section (retired: also the lists no longer asked, for the history)."""
    out = []
    for g in definition(form)["groups"]:
        for s in g["sections"]:
            if s.get("retired") and not retired:
                continue
            for it in s["items"]:
                out.append({**it, "group": g["key"], "section": s["key"], "section_title": s["title"],
                            "section_title_en": s.get("titleEn", s["title"])})
    return out


def _counts(form: str, answers: dict, group: Optional[str] = None) -> dict:
    """UNDER / MEET / EXCEED of a visit over the items asked at its outlet (branch group); score = MEET + EXCEED of the rated."""
    rated = [answers.get(it["key"], {}).get("rating") for it in all_items(form) if it["kind"] == "rating" and applies(it, group)]
    c = {r: sum(1 for x in rated if x == r) for r in RATINGS}
    done = sum(c.values())
    return {"under": c["UNDER"], "meet": c["MEET"], "exceed": c["EXCEED"], "rated": done, "total": len(rated),
            "score": round((c["MEET"] + c["EXCEED"]) * 100 / done, 1) if done else None}


# ---------------------------------------------------------------- visits

COLS = """v.id::text, v.form, v.status, v.user_id::text, v.username, v.full_name, v.branch_code, v.branch_name, v.started_at,
    v.updated_at, v.submitted_at, v.answers, v.counts, v.score, v.latitude, v.longitude, v.accuracy_m, v.outlet_latitude,
    v.outlet_longitude, v.radius_m, v.distance_m, v.outside_radius, v.low_accuracy, v.signed_name, v.signed_at,
    (v.signature IS NOT NULL) AS has_signature"""


def group_of(branch_code: str) -> Optional[str]:
    """The branch group code (CCI, CTG, CTGP …, Monitoring → Branch groups) of an outlet, None when it has none."""
    r = db.fetchrow("""SELECT g.code FROM integration_portal.branch_group_member m JOIN integration_portal.branch_group g ON g.id = m.group_id
                       WHERE m.branch_code = %s""", (branch_code,))
    return r["code"] if r else None


def applies(item: dict, group: Optional[str]) -> bool:
    """Is the item asked at an outlet of this branch group? (an outlet without a group is asked everything)"""
    return not item.get("onlyFor") or group is None or group in item["onlyFor"]


def is_required(item: dict, group: Optional[str]) -> bool:
    """Must the item be rated before sending, at an outlet of this branch group?"""
    if not applies(item, group):
        return False
    return bool(item.get("required", True)) or (group is not None and group in (item.get("requiredFor") or []))


def _num(v: Any) -> Optional[float]:
    return float(v) if v is not None else None


def photos_of(visit_id: str) -> list[dict]:
    rows = db.fetch(f"""SELECT id::text, item_key, taken_at, uploaded_at, latitude, longitude, distance_m, outside_radius
                        FROM {TP} WHERE visit_id::text = %s ORDER BY uploaded_at""", (visit_id,))
    return [{"id": r["id"], "item": r["item_key"], "takenAt": r["taken_at"].isoformat() if r["taken_at"] else None,
             "uploadedAt": r["uploaded_at"].isoformat(), "latitude": _num(r["latitude"]), "longitude": _num(r["longitude"]),
             "distanceM": r["distance_m"], "outsideRadius": r["outside_radius"]} for r in rows]


def _view(r: dict, photos: Optional[list] = None) -> dict:
    out = {
        "id": r["id"], "form": r["form"], "status": r["status"], "userId": r["user_id"], "username": r["username"],
        "name": r["full_name"] or r["username"], "branchCode": r["branch_code"], "branchName": r["branch_name"],
        "startedAt": r["started_at"].isoformat(), "updatedAt": r["updated_at"].isoformat(),
        "submittedAt": r["submitted_at"].isoformat() if r["submitted_at"] else None,
        "counts": r["counts"] or _counts(r["form"], r["answers"] or {}), "score": _num(r["score"]),
        "latitude": _num(r["latitude"]), "longitude": _num(r["longitude"]), "accuracyM": _num(r["accuracy_m"]),
        "outletLatitude": _num(r["outlet_latitude"]), "outletLongitude": _num(r["outlet_longitude"]), "radiusM": r["radius_m"],
        "distanceM": r["distance_m"], "outsideRadius": r["outside_radius"], "lowAccuracy": r["low_accuracy"],
        "signature": {"name": r["signed_name"], "signedAt": r["signed_at"].isoformat() if r["signed_at"] else None}
        if r["has_signature"] else None,
    }
    if photos is not None:
        out["answers"] = r["answers"] or {}
        out["photos"] = photos
    return out


def get_visit(visit_id: str) -> Optional[dict]:
    return db.fetchrow(f"SELECT {COLS} FROM {T} v WHERE v.id::text = %s", (visit_id,))


def google_rating(branch_code: str) -> Optional[dict]:
    """The outlet's latest Google Maps rating (Aggregator → Google Maps, app/gmaps.py); None when not on Google."""
    r = db.fetchrow("SELECT data, fetched_at FROM integration_portal.gmaps_place WHERE branch_code = %s AND place_id IS NOT NULL",
                    (branch_code,))
    d = (r or {}).get("data") or {}
    if d.get("rating") is None:
        return None
    return {"stars": float(d["rating"]), "reviews": d.get("userRatingCount"), "source": "google",
            "fetchedAt": r["fetched_at"].isoformat() if r.get("fetched_at") else None, "mapsUri": d.get("googleMapsUri")}


def _auto_items(form: str) -> list[dict]:
    return [it for it in all_items(form) if it.get("platform") == "google"]


def full(visit_id: str) -> dict:
    row = get_visit(visit_id)
    out = _view(row, photos_of(visit_id))
    out["branchGroup"] = group_of(row["branch_code"])  # which items are required at this outlet
    if row["status"] == "DRAFT" and _auto_items(row["form"]):
        # a draft shows the current figure; it is kept in the answers at sending
        g = google_rating(row["branch_code"])
        out["auto"] = {it["key"]: g for it in _auto_items(row["form"])} if g else {}
    return out


def done_today(form: str, user_id: str) -> dict[str, dict]:
    """{branch code: {id, submittedAt}} of the PIC's visits sent today (WIB)."""
    rows = db.fetch(f"""SELECT DISTINCT ON (branch_code) branch_code, id::text, submitted_at FROM {T}
                        WHERE form = %s AND user_id::text = %s AND status = 'SUBMITTED'
                          AND (submitted_at AT TIME ZONE 'Asia/Jakarta')::date = (now() AT TIME ZONE 'Asia/Jakarta')::date
                        ORDER BY branch_code, submitted_at""", (form, user_id))
    return {r["branch_code"]: {"id": r["id"], "submittedAt": r["submitted_at"].isoformat()} for r in rows}


def _not_today(form: str, user: dict, branch_code: str, branch_name: str) -> None:
    if done_today(form, str(user["id"])).get(branch_code):
        raise VisitError(f"Form untuk {branch_name} sudah dikirim hari ini. Pengisian berikutnya besok.")


def form_state(form: str, user: dict) -> dict:
    """What the form page needs: the account, its branches, its drafts, its latest visits and the branches done today."""
    branches = branch_options(user)
    drafts = db.fetch(f"SELECT {COLS} FROM {T} v WHERE v.form = %s AND v.user_id::text = %s AND v.status = 'DRAFT' ORDER BY v.updated_at DESC",
                      (form, str(user["id"])))
    recent = db.fetch(f"""SELECT {COLS} FROM {T} v WHERE v.form = %s AND v.user_id::text = %s AND v.status = 'SUBMITTED'
                         ORDER BY v.submitted_at DESC LIMIT 10""", (form, str(user["id"])))
    return {"definition": definition(form), "user": {"id": str(user["id"]), "username": user["username"],
            "name": user.get("full_name") or user["username"]}, "branches": branches,
            "drafts": [_view(r) for r in drafts], "recent": [_view(r) for r in recent], "today": done_today(form, str(user["id"]))}


def open_visit(form: str, user: dict, branch_code: str) -> dict:
    """The user's draft for the branch, started when there is none."""
    branch = next((b for b in branch_options(user) if b["code"] == branch_code), None)
    if branch is None:
        raise VisitError("Cabang tidak tersedia untuk akun Anda")
    _not_today(form, user, branch["code"], branch["name"])
    row = db.fetchrow(f"""SELECT id::text FROM {T} WHERE form = %s AND user_id::text = %s AND branch_code = %s AND status = 'DRAFT'""",
                      (form, str(user["id"]), branch["code"]))
    if row is None:
        row = db.fetchrow(
            f"""INSERT INTO {T} (form, user_id, username, full_name, role, branch_code, branch_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (form, user_id, branch_code) WHERE status = 'DRAFT' DO UPDATE SET updated_at = now()
                RETURNING id::text""",
            (form, user["id"], user["username"], user.get("full_name"), user["role"], branch["code"], branch["name"]))
    return full(row["id"])


def own_draft(form: str, user: dict, visit_id: str) -> dict:
    row = get_visit(visit_id)
    if row is None or row["form"] != form or not can_fill(row, user):
        raise LookupError("Kunjungan tidak ditemukan")
    if row["status"] != "DRAFT":
        raise VisitError("Kunjungan ini sudah dikirim")
    return row


def save_answers(form: str, user: dict, visit_id: str, patch: dict) -> dict:
    """Merges {item: {rating?, note?}} into the draft's answers."""
    row = own_draft(form, user, visit_id)
    known = {it["key"]: it for it in all_items(form)}
    answers = dict(row["answers"] or {})
    for key, value in patch.items():
        if key not in known or not isinstance(value, dict):
            raise VisitError(f"Butir tidak dikenal: {key}")
        cur = dict(answers.get(key) or {})
        if "rating" in value:
            rating = value["rating"]
            if rating is not None and (known[key]["kind"] != "rating" or rating not in RATINGS):
                raise VisitError("Penilaian harus UNDER, MEET atau EXCEED")
            cur["rating"] = rating
        if "note" in value:
            cur["note"] = (str(value["note"] or "")).strip()[:1000] or None
        if "stars" in value:
            # typed by the PIC for the delivery apps; Google comes from Google Maps
            if not known[key].get("platform") or known[key]["platform"] == "google":
                raise VisitError("Rating bintang hanya untuk GoFood, GrabFood dan ShopeeFood")
            stars = value["stars"]
            if stars is not None:
                try:
                    stars = round(float(stars), 1)
                except (TypeError, ValueError):
                    raise VisitError("Rating harus angka 1,0 sampai 5,0") from None
                if not 1 <= stars <= 5:
                    raise VisitError("Rating harus angka 1,0 sampai 5,0")
            cur["stars"] = stars
            cur["source"] = "manual" if stars is not None else None
        if "qty" in value:
            # grams (net weight) of Stock Preparation, Espresso and Beans
            if not known[key].get("qty"):
                raise VisitError("Gramasi hanya untuk butir Stock Preparation, Espresso dan Beans")
            qty = value["qty"]
            if qty is not None:
                try:
                    qty = round(float(qty), 1)
                except (TypeError, ValueError):
                    raise VisitError("Gramasi harus angka 0 sampai 1.000.000 gram") from None
                if not 0 <= qty <= MAX_QTY_G:
                    raise VisitError("Gramasi harus angka 0 sampai 1.000.000 gram")
            cur["qty"] = qty
        if form == "wwwr":  # who of the team filled the item
            cur["by"] = user.get("full_name") or user["username"]
        answers[key] = {k: v for k, v in cur.items() if v is not None}
    answers = {k: v for k, v in answers.items() if v}
    counts = _counts(form, answers, group_of(row["branch_code"]))
    db.fetchrow(f"""UPDATE {T} SET answers = %s::jsonb, counts = %s::jsonb, score = %s, updated_at = now()
                    WHERE id::text = %s RETURNING id""", (json.dumps(answers), json.dumps(counts), counts["score"], visit_id))
    return {"answers": answers, "counts": counts}


def _outlet(branch_code: str) -> Optional[dict]:
    return locations([branch_code]).get(branch_code)


def add_photo(form: str, user: dict, visit_id: str, item: str, photo: str, lat: Optional[float], lng: Optional[float],
              taken_at: Optional[datetime]) -> dict:
    row = own_draft(form, user, visit_id)
    if item not in {it["key"] for it in all_items(form)}:
        raise VisitError(f"Butir tidak dikenal: {item}")
    n = db.fetchrow(f"SELECT count(*)::int AS n FROM {TP} WHERE visit_id::text = %s AND item_key = %s", (visit_id, item))["n"]
    if n >= MAX_PHOTOS_PER_ITEM:
        raise VisitError(f"Maksimal {MAX_PHOTOS_PER_ITEM} foto per butir; hapus satu dulu")
    photo_type, data = parse_photo(photo)
    j = judge(lat, lng, None, _outlet(row["branch_code"])) if lat is not None and lng is not None else {"distance_m": None, "outside_radius": False}
    db.fetchrow(
        f"""INSERT INTO {TP} (visit_id, item_key, photo, photo_type, taken_at, latitude, longitude, distance_m, outside_radius, uploaded_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (visit_id, item, data, photo_type, taken_at, lat, lng, j["distance_m"], j["outside_radius"], user["id"]))
    db.fetchrow(f"UPDATE {T} SET updated_at = now() WHERE id::text = %s RETURNING id", (visit_id,))
    return {"photos": photos_of(visit_id)}


def delete_photo(form: str, user: dict, visit_id: str, photo_id: str) -> dict:
    own_draft(form, user, visit_id)
    db.fetchrow(f"DELETE FROM {TP} WHERE id::text = %s AND visit_id::text = %s RETURNING id", (photo_id, visit_id))
    return {"photos": photos_of(visit_id)}


def unrated(form: str, answers: dict, group: Optional[str] = None) -> int:
    return sum(1 for it in all_items(form) if it["kind"] == "rating" and applies(it, group) and not (answers.get(it["key"]) or {}).get("rating"))


def missing(form: str, answers: dict, photos: list[dict], group: Optional[str] = None) -> list[dict]:
    """What blocks the submit (2026-10-27): a required item not rated (need "rating"), or a rated required item without
    the note / photo / grams it asks for (need "note", "photo", "qty" or a combination, e.g. "note+photo"). Optional
    items block nothing; the platform stars of Rating Outlet need neither a note nor a photo. `group` = the outlet's
    branch group (an item may be required only at CTGP / CCI outlets)."""
    with_photo = {p["item"] for p in photos}
    out = []
    for it in all_items(form):
        if it["kind"] != "rating" or not is_required(it, group):
            continue
        a = answers.get(it["key"]) or {}
        if not a.get("rating"):
            out.append({"key": it["key"], "label": it["label"], "labelEn": it.get("labelEn"), "need": "rating"})
            continue
        if it.get("platform"):
            continue
        need = [x for x, ok in (("note", it.get("note") != "required" or bool(a.get("note"))),
                                ("photo", it.get("photo") != "required" or it["key"] in with_photo),
                                ("qty", not it.get("qty") or a.get("qty") is not None)) if not ok]
        if need:
            out.append({"key": it["key"], "label": it["label"], "labelEn": it.get("labelEn"), "need": "+".join(need)})
    return out


def submit(form: str, user: dict, visit_id: str, lat: Optional[float], lng: Optional[float], accuracy: Optional[float],
           device: Optional[str], ip: Optional[str], signature: Optional[str] = None, signed_name: Optional[str] = None) -> dict:
    row = own_draft(form, user, visit_id)
    if form == "wwwr":  # one audit per outlet per day is kept by the schedule; the lead sends
        a = audit_of(visit_id)
        if not a or a["lead_id"] != str(user["id"]):
            raise VisitError("Hanya ketua tim yang dapat mengirim audit WW-WR")
        from app import wwwr
        gaps = wwwr.evaluation_gaps(visit_id)
        if gaps:
            need = {"summary": "evaluasi umum", "revisit": "tanggal kunjungan kembali", "actions": "minimal satu action plan"}
            raise VisitError("Lengkapi evaluasi sebelum mengirim: " + ", ".join(need.get(g, g) for g in gaps))
    else:
        _not_today(form, user, row["branch_code"], row["branch_name"])
    photos = photos_of(visit_id)
    todo = missing(form, row["answers"] or {}, photos, group_of(row["branch_code"]))
    if any(t["need"] == "rating" for t in todo):
        raise VisitError(f"Belum bisa dikirim: {sum(1 for t in todo if t['need'] == 'rating')} butir wajib belum dinilai")
    if todo:
        raise VisitError(f"Belum bisa dikirim: {len(todo)} butir yang dinilai belum lengkap catatan/foto/gram")
    group = group_of(row["branch_code"])
    if unrated(form, row["answers"] or {}, group) == len([i for i in all_items(form) if i["kind"] == "rating" and applies(i, group)]):
        raise VisitError("Belum ada butir yang dinilai")
    # the signature is optional (like the stock opname); a name goes with it
    sig_type, sig = parse_photo(signature) if signature else (None, None)
    signed_name = " ".join((signed_name or "").split())[:120] or None
    if sig is not None and not signed_name:
        raise VisitError("Tulis nama penanda tangan di bawah tanda tangan")
    outlet = _outlet(row["branch_code"]) or {}
    j = judge(lat, lng, accuracy, outlet) if lat is not None and lng is not None else {"distance_m": None, "outside_radius": False, "low_accuracy": False}
    answers = dict(row["answers"] or {})
    auto = _auto_items(form)
    g = google_rating(row["branch_code"]) if auto else None
    for it in auto:
        # the Google rating of the moment of sending stays with the visit
        cur = {k: v for k, v in (answers.get(it["key"]) or {}).items() if k not in ("stars", "reviews", "source", "fetchedAt")}
        answers[it["key"]] = {**cur, **({k: g[k] for k in ("stars", "reviews", "source", "fetchedAt")} if g else {})}
        if not answers[it["key"]]:
            answers.pop(it["key"])
    counts = _counts(form, answers, group)
    db.fetchrow(
        f"""UPDATE {T} SET status = 'SUBMITTED', submitted_at = now(), updated_at = now(), answers = %s::jsonb, counts = %s::jsonb, score = %s,
                latitude = %s, longitude = %s, accuracy_m = %s, outlet_latitude = %s, outlet_longitude = %s, radius_m = %s,
                distance_m = %s, outside_radius = %s, low_accuracy = %s, device = %s, ip = %s,
                signature = %s, signature_type = %s, signed_name = %s, signed_at = CASE WHEN %s THEN now() END
            WHERE id::text = %s RETURNING id""",
        (json.dumps(answers), json.dumps(counts), counts["score"], lat, lng, accuracy, outlet.get("latitude"), outlet.get("longitude"),
         outlet.get("radius_m"), j["distance_m"], j["outside_radius"], j.get("low_accuracy", False), (device or "")[:300] or None,
         ip, sig, sig_type, signed_name if sig is not None else None, sig is not None, visit_id))
    return full(visit_id)


def discard(form: str, user: dict, visit_id: str) -> None:
    own_draft(form, user, visit_id)
    if form == "wwwr":
        raise VisitError("Audit WW-WR tidak dapat dihapus; minta management membatalkan jadwalnya")
    db.fetchrow(f"DELETE FROM {T} WHERE id::text = %s AND status = 'DRAFT' RETURNING id", (visit_id,))


# ---------------------------------------------------------------- monitoring

def monitor(form: str, date_from: date, date_to: date, branches: Optional[list[str]], user_id: Optional[str] = None,
            limit: int = 1000) -> dict:
    """Submitted visits of a period (WIB days) with the figures, the performance per section and the most frequent
    findings (items rated UNDER)."""
    where = ["v.form = %(f)s", "v.status = 'SUBMITTED'", "(v.submitted_at AT TIME ZONE 'Asia/Jakarta')::date BETWEEN %(a)s AND %(b)s"]
    params: dict[str, Any] = {"f": form, "a": date_from, "b": date_to, "lim": limit}
    if branches is not None:
        where.append("v.branch_code = ANY(%(br)s)")
        params["br"] = branches
    if user_id:
        where.append("v.user_id::text = %(u)s")
        params["u"] = user_id
    cond = " AND ".join(where)
    rows = db.fetch(f"""SELECT {COLS}, (SELECT count(*)::int FROM {TP} p WHERE p.visit_id = v.id) AS photo_count
                        FROM {T} v WHERE {cond} ORDER BY v.submitted_at DESC LIMIT %(lim)s""", params)
    items = all_items(form, retired=True)
    sections: dict[str, dict] = {}
    findings: dict[str, dict] = {}
    for r in rows:
        answers = r["answers"] or {}
        for it in items:
            rating = (answers.get(it["key"]) or {}).get("rating")
            if it["kind"] != "rating" or not rating:
                continue
            s = sections.setdefault(it["section"], {"key": it["section"], "title": it["section_title"], "titleEn": it["section_title_en"],
                                                    "group": it["group"], "under": 0, "meet": 0, "exceed": 0})
            s[rating.lower()] += 1
            if rating == "UNDER":
                f = findings.setdefault(it["key"], {"key": it["key"], "label": it["label"], "labelEn": it.get("labelEn"),
                                                    "section": it["section_title"], "sectionEn": it["section_title_en"],
                                                    "count": 0, "branches": set()})
                f["count"] += 1
                f["branches"].add(r["branch_name"])
    data = []
    from app.pic_checkin import groups_of
    group = groups_of(sorted({r["branch_code"] for r in rows}))
    for r in rows:
        v = _view(r)
        v["photoCount"] = r["photo_count"]
        v["branchGroup"] = group.get(r["branch_code"])  # CCI / CTG / CTGP (2026-11-02)
        data.append(v)
    scores = [v["score"] for v in data if v["score"] is not None]
    summary = {"visits": len(data), "branches": len({v["branchCode"] for v in data}), "pics": len({v["userId"] for v in data}),
               "under": sum(v["counts"]["under"] for v in data), "meet": sum(v["counts"]["meet"] for v in data),
               "exceed": sum(v["counts"]["exceed"] for v in data),
               "score": round(sum(scores) / len(scores), 1) if scores else None,
               "outside": sum(1 for v in data if v["outsideRadius"])}
    order = {s["key"]: i for i, s in enumerate(s for g in definition(form)["groups"] for s in g["sections"])}
    # per branch group: visits, outlets, average score, UNDER (2026-11-02)
    per_group: dict[str, dict] = {}
    for v in data:
        x = per_group.setdefault(v["branchGroup"] or "—", {"code": v["branchGroup"] or "—", "visits": 0, "outlets": set(), "scores": [], "under": 0, "pics": set()})
        x["visits"] += 1
        x["outlets"].add(v["branchCode"])
        x["pics"].add(v["userId"])
        x["under"] += v["counts"]["under"]
        if v["score"] is not None:
            x["scores"].append(v["score"])
    by_group = [{"code": x["code"], "visits": x["visits"], "outlets": len(x["outlets"]), "pics": len(x["pics"]), "under": x["under"],
                 "score": round(sum(x["scores"]) / len(x["scores"]), 1) if x["scores"] else None}
                for x in sorted(per_group.values(), key=lambda x: (x["code"] == "—", x["code"]))]
    top = sorted(findings.values(), key=lambda f: (-f["count"], f["label"]))[:15]
    pics = {}
    for v in data:
        pics.setdefault(v["userId"], {"id": v["userId"], "name": v["name"], "n": 0})["n"] += 1
    # stars per platform (Rating Outlet): average of the visits that carry one
    platforms = []
    for it in items:
        if it.get("platform"):
            vals = [float(a["stars"]) for r in rows if (a := (r["answers"] or {}).get(it["key"]) or {}).get("stars") is not None]
            platforms.append({"key": it["key"], "platform": it["platform"], "label": it["label"], "labelEn": it.get("labelEn"),
                              "stars": round(sum(vals) / len(vals), 2) if vals else None, "visits": len(vals)})
    return {"definition": definition(form), "data": data, "summary": summary, "platforms": platforms, "groups": by_group,
            "attendance": attendance(form, date_from, date_to, branches),
            "sections": sorted(sections.values(), key=lambda s: order.get(s["key"], 99)),
            "findings": [{**f, "branches": sorted(f["branches"])} for f in top],
            "pics": sorted(pics.values(), key=lambda p: -p["n"]), "truncated": len(rows) >= limit}


def signature_of(visit_id: str) -> Optional[dict]:
    return db.fetchrow(f"""SELECT id::text, signature AS photo, signature_type AS photo_type, user_id::text, branch_code, form, status
                           FROM {T} WHERE id::text = %s AND signature IS NOT NULL""", (visit_id,))


def photo(photo_id: str) -> Optional[dict]:
    return db.fetchrow(f"""SELECT v.id::text, p.photo, p.photo_type, v.user_id::text, v.branch_code, v.form, v.status
                           FROM {TP} p JOIN {T} v ON v.id = p.visit_id WHERE p.id::text = %s""", (photo_id,))


def attendance(form: str, date_from: date, date_to: date, branches: Optional[list[str]]) -> dict:
    """Per PIC (an active account with the form page and a branch in scope): the days with a visit and the days off (no
    visit at all that day), counted from DAILY_SINCE (the daily rule) and up to yesterday — today is no day off yet."""
    from app import access
    from app.utils import today as wib_today
    now = wib_today()
    if form == "wwwr":  # audits follow a schedule: no daily attendance
        return {"days": [], "today": now.isoformat(), "since": DAILY_SINCE.isoformat(), "pics": []}
    start, end = max(date_from, DAILY_SINCE), min(date_to, now)
    days = [] if end < start else [start + timedelta(days=i) for i in range((end - start).days + 1)]
    users = db.fetch("""SELECT u.id, u.username, u.full_name, u.role,
                                (SELECT array_agg(b.branch_code) FROM integration_portal.user_branch b WHERE b.user_id = u.id) AS codes
                         FROM integration_portal.user_account u WHERE u.is_active AND u.role <> 'superadmin'""")
    scope = None if branches is None else set(branches)
    pics = [u for u in users if u["codes"] and (scope is None or scope & set(u["codes"])) and f"form.{form}" in access.pages_for(u)]
    rows = db.fetch(f"""SELECT user_id::text AS u, (submitted_at AT TIME ZONE 'Asia/Jakarta')::date AS d, count(*)::int AS n FROM {T}
                         WHERE form = %s AND status = 'SUBMITTED' AND (submitted_at AT TIME ZONE 'Asia/Jakarta')::date BETWEEN %s AND %s
                         GROUP BY 1, 2""", (form, date_from, end))
    by: dict[str, dict[str, int]] = {}
    for r in rows:
        by.setdefault(r["u"], {})[r["d"].isoformat()] = r["n"]
    out = []
    for u in pics:
        mine = by.get(str(u["id"]), {})
        active = sum(1 for d in days if d.isoformat() in mine)
        off = sum(1 for d in days if d < now and d.isoformat() not in mine)
        out.append({"id": str(u["id"]), "username": u["username"], "name": u["full_name"] or u["username"], "branches": len(u["codes"]),
                    "active": active, "off": off, "visits": sum(mine.values()), "days": mine})
    out.sort(key=lambda x: (-x["off"], x["name"].lower()))
    return {"days": [d.isoformat() for d in days], "today": now.isoformat(), "since": DAILY_SINCE.isoformat(), "pics": out}
