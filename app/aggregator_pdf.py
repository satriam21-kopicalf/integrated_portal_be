"""Aggregator Insight report as a PDF (2026-11-27, Aggregator team & management): a period (and a platform) read from the
same data as the pages — reviews, complaints, void, opening & closing, out of stock, the status now — written as an
executive summary, charts, the analysis with its recommendations (rules on the figures, `recommend`, tested) and the
outlets that need attention. Generated on demand from the data of the moment; nothing is typed by hand.
"""
import io
from datetime import date, datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from reportlab.graphics.charts.barcharts import HorizontalBarChart, VerticalBarChart
from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import CondPageBreak, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

WIB = ZoneInfo("Asia/Jakarta")
LOGO = __import__("pathlib").Path(__file__).parent / "static" / "calf-logo.png"  # 782 x 414, the Kopi Calf logo
COMPANY = "Calf Coffee & Milkbar"
# Atlassian colours (the Jira look of the portal)
INK, SUBTLE, LINE, BLUE = colors.HexColor("#172B4D"), colors.HexColor("#626F86"), colors.HexColor("#DCDFE4"), colors.HexColor("#0B1385")  # BLUE: the Kopi Calf navy
RED, AMBER, GREEN, SUNKEN = colors.HexColor("#C9372C"), colors.HexColor("#B38600"), colors.HexColor("#1F845A"), colors.HexColor("#F7F8F9")
PLATFORM_COLOR = {"GoFood": colors.HexColor("#00AA13"), "GrabFood": colors.HexColor("#00B14F"), "ShopeeFood": colors.HexColor("#EE4D2D")}

S = {
    "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=INK),
    "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=12.5, leading=16, textColor=INK, spaceBefore=10, spaceAfter=4),
    "p": ParagraphStyle("p", fontName="Helvetica", fontSize=9, leading=12.5, textColor=INK),
    "small": ParagraphStyle("small", fontName="Helvetica", fontSize=7.5, leading=10, textColor=SUBTLE),
    "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8, leading=10, textColor=INK),
}


def _nf(v: Any, d: int = 0) -> str:
    if v is None:
        return "—"
    s = f"{v:,.{d}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _rp(v: Any) -> str:
    return "—" if v is None else f"Rp {_nf(v)}"


def _pct(a: float, b: float) -> Optional[float]:
    return round(a / b * 100, 1) if b else None


# ---------------------------------------------------------------- the figures & the recommendations

def facts(a: date, z: date, platform: Optional[str]) -> dict:
    """Every figure of the report, from the functions of the pages."""
    from app import aggregator_quality as q
    from app import aggregator_reports as r
    rv, cp, vd, op, oo = q.reviews(a, z, platform), q.complaints(a, z, platform), q.voids(a, z, platform), r.opening(a, z, platform), r.oos(a, z, platform)
    now = r.now_status()
    hold = [x for x in now["data"] if x["state"] == "HOLD" and (not platform or x["platform"] == platform)]
    so = op["summary"]
    late_by_outlet: dict[str, dict] = {}
    for x in op["rows"]:
        o = late_by_outlet.setdefault(x["outlet"], {"outlet": x["outlet"], "days": 0, "late": 0, "notOpened": 0, "early": 0, "pause": 0})
        o["days"] += 1
        o["late"] += x["status"] == "late"
        o["notOpened"] += x["status"] == "not_opened"
        o["early"] += bool(x.get("earlyMin"))
        o["pause"] += x.get("pauseMin") or 0
    return {
        "period": (a, z), "platform": platform,
        "reviews": rv["summary"], "byPlatform": rv["byPlatform"], "reviewDays": rv["days"], "topics": rv["topics"],
        "worstOutlets": sorted([o for o in rv["byOutlet"] if o["low"]], key=lambda o: (-o["low"], o["avg"] or 5))[:10],
        "complaints": cp["summary"], "complaintCats": cp["categories"],
        "void": vd["summary"], "voidOutlets": sorted(vd["byOutlet"], key=lambda o: -o["n"])[:10],
        "voidDays": _per_day(vd["data"], a, z),
        "opening": so, "lateOutlets": sorted(late_by_outlet.values(), key=lambda o: (-(o["late"] + o["notOpened"] * 2), o["outlet"]))[:10],
        "oos": oo["summary"], "oosCats": oo["byCategory"][:8], "oosOutlets": oo["byOutlet"][:10],
        "now": now["count"], "hold": hold, "stale": now["stale"], "unmapped": now["unmapped"],
    }


def _per_day(rows: list[dict], a: date, z: date) -> list[tuple[str, int]]:
    n: dict[str, int] = {}
    for x in rows:
        n[x["day"]] = n.get(x["day"], 0) + 1
    out, d = [], a
    while d <= z:
        out.append((d.isoformat(), n.get(d.isoformat(), 0)))
        d += timedelta(days=1)
    return out


def recommend(f: dict) -> list[tuple[str, str, str]]:
    """(HIGH | MEDIUM | LOW, the finding, what to do), most urgent first. Pure (tested): thresholds agreed for the team."""
    out: list[tuple[str, str, str]] = []
    if f["hold"]:
        names = ", ".join(f"{x['branchName'] or x['name']} ({x['platform']})" for x in f["hold"][:5])
        out.append(("HIGH", f"{len(f['hold'])} outlet x platform HOLD sekarang: tutup padahal masih jam operasional",
                    f"Buka kembali di aplikasi merchant atau konfirmasi ke outlet: {names}."))
    so = f["opening"]
    on = _pct(so["onTime"], so["counted"])
    if on is not None and on < 80:
        out.append(("HIGH" if on < 60 else "MEDIUM", f"Buka tepat waktu baru {_nf(on, 1)}% ({_nf(so['late'])} hari-outlet terlambat, {_nf(so['notOpened'])} tidak buka)",
                    "Bahas dengan outlet teratas di tabel keterlambatan; cek jam di aplikasi merchant sudah sesuai jadwal outlet (Opening & Closing → Jam aplikasi merchant)."))
    if so.get("earlyClose"):
        out.append(("MEDIUM", f"{_nf(so['earlyClose'])} kali tutup lebih awal, total jeda {_nf((so.get('pauseMin') or 0) / 60, 1)} jam",
                    "Cari pola per jam dan outlet; jeda panjang di jam ramai berarti order hilang."))
    v = f["void"]
    if v["n"]:
        no = _pct(v["noReason"], v["n"])
        if no and no > 20:
            out.append(("HIGH" if no > 50 else "MEDIUM", f"{_nf(no, 1)}% void ({_nf(v['noReason'])} dari {_nf(v['n'])}) belum diberi alasan",
                        "Isi alasan di Void Order setiap hari: tanpa alasan, void tidak dapat dicegah atau dibebankan."))
        if v.get("pct") and v["pct"] > 2:
            out.append(("MEDIUM", f"Void {_nf(v['pct'], 2)}% dari order selesai (nilai {_rp(v['amount'])})", "Telusuri outlet dengan void terbanyak dan alasannya."))
    for p in sorted(f.get("byPlatform") or [], key=lambda x: x.get("avg") or 5):
        if p.get("avg") is not None and p["avg"] < 4.5 and p.get("n", 0) >= 20:
            out.append(("HIGH" if p["avg"] < 4 else "MEDIUM", f"Rating {p['platform']} {_nf(p['avg'], 2)} dari {_nf(p['n'])} ulasan, jauh di bawah platform lain",
                        f"Baca ulasan rendah {p['platform']} per outlet (Ulasan & Rating, filter platform) dan cek apakah masalahnya produk, kemasan atau pengantaran."))
    rv = f["reviews"]
    if rv["low"]:
        no_ticket = rv.get("noTicket", 0)
        if no_ticket:
            out.append(("MEDIUM" if no_ticket < 50 else "HIGH", f"{_nf(no_ticket)} ulasan rendah belum ditangani",
                        "Tangani dari Ulasan & Rating → Tangani: lampirkan bukti dan balas ulasan di aplikasi merchant."))
        if f["topics"]:
            t = f["topics"][0]
            out.append(("MEDIUM", f"Topik ulasan rendah terbanyak: {t['topic']} ({_nf(t['n'])})", "Bawa ke briefing outlet dan QA untuk topik ini."))
    c = f["complaints"]
    if c.get("open") or c.get("progress"):
        out.append(("MEDIUM", f"{_nf((c.get('open') or 0) + (c.get('progress') or 0))} komplain belum selesai", "Selesaikan dengan bukti penanganan; tiket tanpa bukti tidak dapat ditutup."))
    oo = f["oos"]
    if oo.get("outNow"):
        out.append(("LOW" if oo["outNow"] < 200 else "MEDIUM", f"{_nf(oo['outNow'])} item habis sekarang di {_nf(oo.get('outletsWithOos'))} outlet",
                    "Aktifkan kembali item yang sudah tersedia; item yang lama nonaktif dirapikan dari menu."))
    if f["unmapped"]:
        out.append(("LOW", f"{_nf(f['unmapped'])} channel belum dicocokkan ke outlet", "Cocokkan di Ringkasan agar datanya masuk ke laporan per outlet."))
    if f["stale"]:
        out.append(("LOW", f"{_nf(f['stale'])} bacaan BotFood lebih dari 2 jam", "Status dapat tertinggal: cek aplikasi merchant sebelum bertindak."))
    rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    return sorted(out, key=lambda x: rank[x[0]])


# ---------------------------------------------------------------- charts

def _bars(title: str, labels: list[str], values: list[float], color=BLUE, w: float = 85 * mm, h: float = 55 * mm, horizontal: bool = False,
          fmt: str = "%d", top: Optional[float] = None) -> Drawing:
    d = Drawing(w, h)
    d.add(String(0, h - 9, title, fontName="Helvetica-Bold", fontSize=8.5, fillColor=INK))
    c = HorizontalBarChart() if horizontal else VerticalBarChart()
    c.x, c.y, c.width, c.height = (70 if horizontal else 24), 16, w - (80 if horizontal else 32), h - 34
    c.data = [values or [0]]
    c.categoryAxis.categoryNames = [x[:22] for x in labels] or ["—"]
    c.categoryAxis.labels.fontSize = 6.5
    c.categoryAxis.labels.fillColor = SUBTLE
    if not horizontal:
        c.categoryAxis.labels.angle = 35 if len(labels) > 6 else 0
        c.categoryAxis.labels.dy = -6 if len(labels) > 6 else -2
        c.categoryAxis.labels.boxAnchor = "ne" if len(labels) > 6 else "n"
    c.valueAxis.labels.fontSize = 6.5
    c.valueAxis.labels.fillColor = SUBTLE
    c.valueAxis.valueMin = 0
    if top is not None:
        c.valueAxis.valueMax = top
    c.valueAxis.gridStrokeColor = LINE
    c.valueAxis.visibleGrid = True
    c.valueAxis.strokeColor = LINE
    c.categoryAxis.strokeColor = LINE
    c.bars[0].fillColor = color
    c.bars[0].strokeColor = None
    c.barLabelFormat = (lambda v: _nf(v, 2)) if fmt == "%.2f" else fmt
    c.barLabels.fontSize = 6
    c.barLabels.fillColor = INK
    c.barLabels.nudge = 6 if horizontal else 5
    d.add(c)
    return d


def _line(title: str, labels: list[str], values: list[float], w: float = 85 * mm, h: float = 55 * mm) -> Drawing:
    d = Drawing(w, h)
    d.add(String(0, h - 9, title, fontName="Helvetica-Bold", fontSize=8.5, fillColor=INK))
    lp = LinePlot()
    lp.x, lp.y, lp.width, lp.height = 26, 18, w - 34, h - 36
    lp.data = [list(enumerate(values or [0]))]
    lp.lines[0].strokeColor = BLUE
    lp.lines[0].strokeWidth = 1.6
    lp.xValueAxis.valueMin, lp.xValueAxis.valueMax = 0, max(1, len(values) - 1)
    lp.xValueAxis.valueSteps = list(range(len(values)))
    lp.xValueAxis.labelTextFormat = lambda i: labels[int(i)][8:10] + "/" + labels[int(i)][5:7] if 0 <= int(i) < len(labels) else ""
    lp.xValueAxis.labels.fontSize = 6.5
    lp.yValueAxis.labels.fontSize = 6.5
    lp.yValueAxis.valueMin = 0
    lp.yValueAxis.visibleGrid = True
    lp.yValueAxis.gridStrokeColor = LINE
    for ax in (lp.xValueAxis, lp.yValueAxis):
        ax.strokeColor = LINE
        ax.labels.fillColor = SUBTLE
    d.add(lp)
    return d


# ---------------------------------------------------------------- the document

def _kpis(rows: list[tuple[str, str, str]]) -> Table:
    cells = [[Paragraph(f'<font size="7.5" color="#626F86">{l}</font><br/><font size="13" color="#172B4D"><b>{v}</b></font><br/><font size="7" color="#626F86">{s}</font>', S["p"])
              for l, v, s in rows[i:i + 4]] for i in range(0, len(rows), 4)]
    for r in cells:
        r += [""] * (4 - len(r))
    t = Table(cells, colWidths=[45 * mm] * 4)
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("LEFTPADDING", (0, 0), (-1, -1), 7)]))
    return t


def _table(head: list[str], rows: list[list[Any]], widths: list[float]) -> Table:
    data = [[Paragraph(f"<b>{h}</b>", S["small"]) for h in head]] + [[Paragraph(str(c), S["cell"]) for c in r] for r in rows]
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 1, LINE), ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("LEFTPADDING", (0, 0), (-1, -1), 4)]))
    return t


def _side(*charts: Drawing) -> Table:
    t = Table([list(charts)], colWidths=[90 * mm] * len(charts))
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    return t


def build(a: date, z: date, platform: Optional[str] = None) -> bytes:
    f = facts(a, z, platform)
    recs = recommend(f)
    buf = io.BytesIO()
    made = datetime.now(WIB)
    title = f"Laporan Aggregator Insight · {a:%d/%m/%Y} – {z:%d/%m/%Y}{' · ' + platform if platform else ''}"

    def page(c, doc):
        """The letterhead (2026-11-27): the Kopi Calf logo, the company and the report, its period and when it was made;
        a blue rule under it; the footer with the page and the note that the report is internal."""
        c.saveState()
        top = 297 * mm
        if LOGO.is_file():
            c.drawImage(str(LOGO), 15 * mm, top - 24 * mm, width=30 * mm, height=30 * mm * 414 / 782, mask="auto")
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 13)
        c.drawRightString(195 * mm, top - 13 * mm, "Laporan Aggregator Insight")
        c.setFont("Helvetica", 8.5)
        c.setFillColor(SUBTLE)
        c.drawRightString(195 * mm, top - 18 * mm, f"{COMPANY} · Tim Aggregator")
        c.drawRightString(195 * mm, top - 22.5 * mm, f"Periode {a:%d %b %Y} – {z:%d %b %Y}{' · ' + platform if platform else ' · semua platform'}")
        c.setStrokeColor(BLUE)
        c.setLineWidth(2.2)
        c.line(15 * mm, top - 27 * mm, 195 * mm, top - 27 * mm)
        c.setStrokeColor(LINE)
        c.setLineWidth(0.6)
        c.line(15 * mm, top - 28.2 * mm, 195 * mm, top - 28.2 * mm)
        c.line(15 * mm, 13 * mm, 195 * mm, 13 * mm)
        c.setFont("Helvetica", 7)
        c.setFillColor(SUBTLE)
        c.drawString(15 * mm, 9 * mm, f"Dokumen internal {COMPANY} · dibuat otomatis {made:%d/%m/%Y %H:%M} WIB dari data portal")
        c.drawRightString(195 * mm, 9 * mm, f"Halaman {doc.page}")
        c.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=34 * mm, bottomMargin=17 * mm, title=title, author="Portal Kopi Calf")
    rv, so, v, c, oo = f["reviews"], f["opening"], f["void"], f["complaints"], f["oos"]
    on = _pct(so["onTime"], so["counted"])
    el: list = [Paragraph(f"Dibuat {made:%d %B %Y, %H:%M} WIB · semua jam dalam WIB", S["small"]), Spacer(0, 4)]

    el.append(Paragraph("Ringkasan", S["h2"]))
    el.append(_kpis([
        ("Status sekarang", f"{_nf(f['now']['OPEN'])} buka", f"{_nf(f['now']['HOLD'])} hold · {_nf(f['now']['CLOSE'])} tutup"),
        ("Buka tepat waktu", f"{_nf(on, 1) if on is not None else '—'}%", f"{_nf(so['late'])} terlambat · {_nf(so['notOpened'])} tidak buka"),
        ("Rating rata-rata", _nf(rv["avg"], 2) if rv["avg"] else "—", f"{_nf(rv['n'])} ulasan · {_nf(rv['low'])} rendah"),
        ("Komplain", _nf(c["n"]), f"{_nf(c.get('done'))} selesai · {_nf((c.get('open') or 0) + (c.get('progress') or 0))} berjalan"),
        ("Void", _nf(v["n"]), f"{_nf(v.get('pct') or 0, 2)}% order · {_rp(v['amount'])}"),
        ("Void tanpa alasan", _nf(v["noReason"]), f"{_nf(_pct(v['noReason'], v['n']) or 0, 1)}% dari void"),
        ("Item habis sekarang", _nf(oo.get("outNow")), f"{_nf(oo.get('outletsWithOos'))} outlet"),
        ("Tutup lebih awal", _nf(so.get("earlyClose")), f"jeda {_nf((so.get('pauseMin') or 0) / 60, 1)} jam"),
    ]))

    el.append(Paragraph("Analisis & rekomendasi", S["h2"]))
    if recs:
        tone = {"HIGH": ("#FFECEB", "#AE2E24", "PRIORITAS"), "MEDIUM": ("#FFF7D6", "#7F5F01", "PERLU"), "LOW": ("#EEF0FB", "#080E63", "INFO")}
        rows = [[Paragraph(f'<font color="{tone[s][1]}"><b>{tone[s][2]}</b></font>', S["small"]),
                 Paragraph(f"<b>{t}</b><br/>{d}", S["p"])] for s, t, d in recs]
        tb = Table(rows, colWidths=[22 * mm, 158 * mm])
        tb.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
                                ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)] +
                               [("BACKGROUND", (0, i), (0, i), colors.HexColor(tone[s][0])) for i, (s, _, _) in enumerate(recs)]))
        el.append(tb)
    else:
        el.append(Paragraph("Tidak ada temuan yang perlu ditindaklanjuti pada periode ini.", S["p"]))

    el += [CondPageBreak(70 * mm), Paragraph("Chart", S["h2"])]
    plats = f["byPlatform"]
    el.append(_side(_bars("Rating rata-rata per platform", [p["platform"] for p in plats], [p["avg"] or 0 for p in plats], fmt="%.2f", top=5),
                    _bars("Ulasan rendah per platform", [p["platform"] for p in plats], [p["low"] for p in plats], RED)))
    el.append(Spacer(0, 4))
    el.append(_side(_line("Ulasan per hari", [d["day"] for d in f["reviewDays"]], [d["n"] for d in f["reviewDays"]]),
                    _line("Void per hari", [d for d, _ in f["voidDays"]], [n for _, n in f["voidDays"]])))
    el.append(Spacer(0, 4))
    el.append(_side(_bars("Topik ulasan rendah", [t["topic"] for t in f["topics"][:8]], [t["n"] for t in f["topics"][:8]], AMBER, horizontal=True, h=62 * mm),
                    _bars("Opening & closing (hari-outlet)", ["Tepat waktu", "Terlambat", "Tidak buka", "Tutup awal"],
                          [so["onTime"], so["late"], so["notOpened"], so.get("earlyClose") or 0], GREEN, h=62 * mm)))

    el += [CondPageBreak(60 * mm), Paragraph("Outlet yang perlu perhatian", S["h2"])]
    if f["hold"]:
        el.append(KeepTogether([Paragraph("HOLD sekarang (tutup di dalam jam operasional)", S["p"]), Spacer(0, 2),
                                _table(["Outlet", "Platform", "Jam hari ini", "Sejak"],
                                       [[x["branchName"] or x["name"], x["platform"], x["hours"] or "—",
                                         datetime.fromisoformat(x["since"]).astimezone(WIB).strftime("%d/%m %H:%M") if x["since"] else "—"] for x in f["hold"]],
                                       [70 * mm, 30 * mm, 45 * mm, 35 * mm]), Spacer(0, 6)]))
    el.append(KeepTogether([Paragraph("Keterlambatan buka (terbanyak)", S["p"]), Spacer(0, 2),
                            _table(["Outlet", "Hari × platform", "Terlambat", "Tidak buka", "Tutup awal", "Jeda (jam)"],
                                   [[o["outlet"], o["days"], o["late"], o["notOpened"], o["early"], _nf(o["pause"] / 60, 1)] for o in f["lateOutlets"]],
                                   [70 * mm, 18 * mm, 22 * mm, 22 * mm, 22 * mm, 26 * mm]), Spacer(0, 6)]))
    el.append(KeepTogether([Paragraph("Ulasan rendah terbanyak", S["p"]), Spacer(0, 2),
                            _table(["Outlet", "Ulasan", "Rendah", "Rata-rata"], [[o["outlet"], _nf(o["n"]), _nf(o["low"]), _nf(o["avg"], 2)] for o in f["worstOutlets"]],
                                   [100 * mm, 25 * mm, 25 * mm, 30 * mm]), Spacer(0, 6)]))
    el.append(KeepTogether([Paragraph("Void terbanyak", S["p"]), Spacer(0, 2),
                            _table(["Outlet", "Void", "Nilai", "Tanpa alasan"], [[o["outlet"], _nf(o["n"]), _rp(o["amount"]), _nf(o["noReason"])] for o in f["voidOutlets"]],
                                   [90 * mm, 25 * mm, 40 * mm, 25 * mm])]))
    el += [Spacer(0, 8), Paragraph("Sumber: BotFood (status, jam, ulasan, item) dan ESB (void) seperti yang tampil di portal. Keterlambatan dan tutup awal adalah "
                                   "batas atas menurut bacaan BotFood (setiap ±1 jam); order platform membuktikan outlet sudah buka. Cek aplikasi merchant sebelum menilai anggota tim.", S["small"])]
    doc.build(el, onFirstPage=page, onLaterPages=page)
    return buf.getvalue()
