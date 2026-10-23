"""The documents of a Damaged Equipment request (app/damaged_equipment.py) as PDF, with the Kopi Calf logo:
  * the request report (any time; the share link's copy without invoices): title with status and urgency, the steps, the
    request data, items, analysis, photos, handling, handover, signatures, history with notes, time per stage, QR code;
  * the final documents once Done (build_final): an internal work report (everything above) or, for a vendor, the
    handover record ("Berita Acara Penyelesaian Pekerjaan Vendor"): the request, the vendor, the cost with its invoices,
    the handover photos and the signatures of the vendor, the PIC and the maintenance team — no handling history."""
import io
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app import damaged_equipment as de

WIB = ZoneInfo("Asia/Jakarta")
HEX = {"ink": "#0f172a", "muted": "#64748b", "line": "#e2e8f0", "soft": "#f8fafc", "head": "#f1f5f9", "brand": "#1d4ed8", "navy": "#0b1f4d"}
INK, MUTED, LINE, SOFT, HEAD, BRAND, NAVY = (colors.HexColor(HEX[k]) for k in ("ink", "muted", "line", "soft", "head", "brand", "navy"))
CAUSE = {"WEAR": "Pemakaian normal", "MISUSE": "Salah pemakaian", "AGE": "Umur barang", "OTHER": "Lainnya"}
EVENT = {"SUBMIT": "Pengajuan dikirim", "ASSIGN": "Tukang ditugaskan", "RELEASE": "Tukang dilepas", "SWAP": "Pergeseran tukang",
         "COST": "Biaya diperbarui", "NOTE": "Catatan tim", "DUPLICATE": "Ditandai duplikat", "WORK": "Link foto tukang",
         "TARGET": "Target waktu disesuaikan", "HANDOVER": "Serah terima dari PIC"}
# (background, text) of the status / urgency pills
STATUS_PILL = {"PENDING": ("#f1f5f9", "#334155"), "VALIDATION": ("#e0f2fe", "#075985"), "REVIEW": ("#ede9fe", "#5b21b6"),
               "ON_HOLD": ("#fef3c7", "#92400e"), "ACCEPTED": ("#dbeafe", "#1e40af"), "IN_PROGRESS": ("#dbeafe", "#1e3a8a"),
               "DONE": ("#dcfce7", "#166534"), "REJECTED": ("#fee2e2", "#991b1b")}
URGENCY_PILL = {"CRITICAL": ("#fee2e2", "#991b1b"), "HIGH": ("#ffedd5", "#9a3412"), "NORMAL": ("#dbeafe", "#1e40af"),
                "LOW": ("#f1f5f9", "#334155"), "PLANNED": ("#ede9fe", "#5b21b6")}
STATUS_PILL["HANDOVER"] = ("#cffafe", "#155e75")
STEPS = list(de.STEPS)
LOGO = Path(__file__).parent / "static" / "calf-logo.png"
MARGIN = 16 * mm
WIDTH = A4[0] - 2 * MARGIN


def _wib(v: Optional[str]) -> str:
    if not v:
        return "-"
    return datetime.fromisoformat(v).astimezone(WIB).strftime("%d/%m/%Y %H:%M") + " WIB"


def _hours(h: Optional[float]) -> str:
    if h is None:
        return "-"
    if h < 1:
        return f"{round(h * 60)} menit"
    return f"{h:.1f} jam" if h < 48 else f"{h / 24:.1f} hari"


def _rp(v: Optional[float]) -> str:
    return "-" if v is None else f"Rp {v:,.0f}".replace(",", ".")


def _esc(s: Optional[str]) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")


def _image(path, max_w: float, max_h: float) -> Optional[Image]:
    try:
        reader = ImageReader(str(path))
        w, h = reader.getSize()
    except Exception:  # noqa: BLE001 - unreadable image: left out
        return None
    scale = min(max_w / w, max_h / h, 1.0)
    return Image(str(path), width=w * scale, height=h * scale)


def _qr(url: str, size: float) -> Drawing:
    w = QrCodeWidget(url)
    x1, y1, x2, y2 = w.getBounds()
    d = Drawing(size, size, transform=[size / (x2 - x1), 0, 0, size / (y2 - y1), 0, 0])
    d.add(w)
    return d


class _S:
    """Paragraph styles of the report."""

    def __init__(self):
        base = getSampleStyleSheet()["Normal"]
        self.body = ParagraphStyle("b", parent=base, fontName="Helvetica", fontSize=9, leading=12.5, textColor=INK)
        self.small = ParagraphStyle("s", parent=self.body, fontSize=7.5, leading=10, textColor=MUTED)
        self.label = ParagraphStyle("l", parent=self.small, fontName="Helvetica-Bold", fontSize=6.8, leading=9)
        self.value = ParagraphStyle("v", parent=self.body, fontName="Helvetica-Bold", fontSize=9.5, leading=12.5)
        self.title = ParagraphStyle("t", parent=self.body, fontName="Helvetica-Bold", fontSize=17, leading=21)
        self.th = ParagraphStyle("th", parent=self.small, fontName="Helvetica-Bold", textColor=colors.white)
        self.right = ParagraphStyle("r", parent=self.body, alignment=TA_RIGHT)
        self.center = ParagraphStyle("c", parent=self.small, alignment=TA_CENTER)
        self.pill = ParagraphStyle("p", parent=self.small, fontName="Helvetica-Bold", fontSize=8, alignment=TA_CENTER)


def _pill(text: str, tone: tuple[str, str], s: _S, width: float) -> Table:
    return Table([[Paragraph(f"<font color='{tone[1]}'>{_esc(text)}</font>", s.pill)]], colWidths=[width],
                 style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(tone[0])), ("ROUNDEDCORNERS", [6, 6, 6, 6]),
                                   ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))


def _section(n: int, title: str, s: _S, hint: str = "") -> Table:
    """A numbered section heading: a blue number chip, the title and a thin rule."""
    chip = Table([[Paragraph(f"<font color='white'><b>{n}</b></font>", s.pill)]], colWidths=[6 * mm], rowHeights=[6 * mm],
                 style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), BRAND), ("ROUNDEDCORNERS", [3, 3, 3, 3]),
                                   ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    text = Paragraph(f"<b>{_esc(title)}</b>" + (f"  <font size=7.5 color='{HEX['muted']}'>{_esc(hint)}</font>" if hint else ""),
                     ParagraphStyle("sec", parent=s.body, fontSize=11, leading=14))
    head = Table([[chip, text]], colWidths=[9 * mm, WIDTH - 9 * mm],
                 style=TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LINEBELOW", (0, 0), (-1, -1), 0.6, LINE),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 10)]))
    head.keepWithNext = True  # never alone at the bottom of a page
    return head


def _grid(cells: list[tuple[str, str]], s: _S, cols: int = 4) -> Table:
    """Key facts as boxed cells (label above, value below)."""
    rows = []
    for i in range(0, len(cells), cols):
        chunk = cells[i:i + cols] + [("", "")] * (cols - len(cells[i:i + cols]))
        rows.append([[Paragraph(_esc(k).upper(), s.label), Paragraph(_esc(v) if v else "", s.value)] if k else "" for k, v in chunk])
    return Table(rows, colWidths=[WIDTH / cols] * cols,
                 style=TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.6, LINE),
                                   ("BACKGROUND", (0, 0), (-1, -1), SOFT), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 8)]))


def _steps(r: dict, s: _S) -> Table:
    at = r["holdFrom"] if r["status"] == "ON_HOLD" else r["status"]
    idx = len(STEPS) - 1 if r["status"] == "DONE" else (STEPS.index(at) if at in STEPS else -1)
    cells, style = [], [("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                        ("ROUNDEDCORNERS", [4, 4, 4, 4])]
    for i, st in enumerate(STEPS):
        done, here = i < idx or r["status"] == "DONE", i == idx and r["status"] != "DONE"
        bg, fg = ("#1d4ed8", "#ffffff") if done else (STATUS_PILL.get(r["status"], ("#dbeafe", "#1e3a8a")) if here else ("#f1f5f9", "#94a3b8"))
        cells.append(Paragraph(f"<font color='{fg}'><b>{i + 1}. {_esc(de.STATUS_LABEL[st])}</b></font>", s.center))
        style.append(("BACKGROUND", (i, 0), (i, 0), colors.HexColor(bg)))
    style += [("LINEAFTER", (i, 0), (i, 0), 2, colors.white) for i in range(len(STEPS) - 1)]
    return Table([cells], colWidths=[WIDTH / len(STEPS)] * len(STEPS), style=TableStyle(style))


def _table(head: list[str], rows: list[list], widths: list[float], s: _S) -> Table:
    data = [[Paragraph(h, s.th) for h in head]] + rows
    style = [("BACKGROUND", (0, 0), (-1, 0), NAVY), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE),
             ("BOX", (0, 0), (-1, -1), 0.6, LINE), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]
    style += [("BACKGROUND", (0, i), (-1, i), SOFT) for i in range(2, len(data), 2)]
    return Table(data, colWidths=[WIDTH * w for w in widths], repeatRows=1, style=TableStyle(style))


def _photos(media: list[dict], s: _S, caption) -> Optional[Table]:
    cells = []
    for m in media:
        f = de.media_file(m["id"])
        img = _image(f["path"], WIDTH / 3 - 8, 52 * mm) if f and f["path"] else None
        if img:
            cells.append(Table([[img], [Paragraph(caption(m), s.small)]], style=TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0)])))
    if not cells:
        return None
    grid = [cells[i:i + 3] for i in range(0, len(cells), 3)]
    grid[-1] += [""] * (3 - len(grid[-1]))
    return Table(grid, colWidths=[WIDTH / 3] * 3, style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))


def _event_text(e: dict) -> str:
    d = e["details"] or {}
    if e["kind"] == "STATUS":
        return f"{de.STATUS_LABEL.get(e['from'] or '', e['from'] or '')} → {de.STATUS_LABEL.get(e['to'] or '', e['to'] or '')}"
    if e["kind"] == "COST" and d.get("invoice") is not None:
        return f"Invoice dilampirkan · {d['invoice']}"
    if e["kind"] == "COST" and d.get("labor"):
        return f"Upah {d['labor']} " + (f"disesuaikan → {_rp(d.get('adjust'))}" if d.get("adjust") is not None else "kembali dihitung otomatis")
    if e["kind"] == "COST" and d.get("item"):
        verb = {"add": "Biaya ditambahkan", "edit": "Biaya diubah", "delete": "Biaya dihapus"}.get(d.get("action"), "Biaya")
        return f"{verb} · {d['item']}" + (f" · estimasi {_rp(d.get('estAmount'))}, aktual {_rp(d.get('actualAmount'))}" if d.get("action") != "delete" else "")
    if e["kind"] == "COST":
        return f"Biaya diperbarui · estimasi {_rp(d.get('estCost'))}, aktual {_rp(d.get('actualCost'))}"
    if e["kind"] == "WORK":
        return (f"Foto pengerjaan dikirim tukang · {d.get('photos', 0)} foto" if d.get("link") == "sent"
                else f"Link foto dikirim ke tukang · {d.get('technician', '')}")
    if e["kind"] == "DUPLICATE":
        return f"Ditandai duplikat dari {d.get('of', '')}"
    if e["kind"] == "TARGET":
        return f"Target waktu disesuaikan → {_wib(d.get('to'))}"
    if e["kind"] == "HANDOVER":
        return f"Serah terima dari PIC · {d.get('photos', 0)} foto" + (f" · ttd vendor {d['vendor']}" if d.get("vendor") else "")
    text = EVENT.get(e["kind"], e["kind"])
    if d.get("technician"):
        text += f" · {d['technician']}"
    if e["kind"] == "SWAP" and (d.get("to") or d.get("from")):
        text += f" ({'ke ' + d['to'] if d.get('to') else 'dari ' + d['from']})"
    return text


def _sign_box(title: str, img_bytes: Optional[bytes], name: str, when: str, s: _S, width: float) -> Table:
    try:
        img = Image(io.BytesIO(img_bytes), width=width - 16, height=20 * mm, kind="proportional") if img_bytes else Spacer(1, 20 * mm)
    except Exception:  # noqa: BLE001
        img = Spacer(1, 20 * mm)
    return Table([[Paragraph(_esc(title).upper(), s.label)], [img], [Paragraph(f"<b>{_esc(name or '-')}</b>", s.body)], [Paragraph(_esc(when), s.small)]],
                 colWidths=[width], style=TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINE), ("LINEABOVE", (0, 2), (-1, 2), 0.6, LINE),
                                                      ("LEFTPADDING", (0, 0), (-1, -1), 8), ("VALIGN", (0, 0), (-1, -1), "TOP")]))


def build_final(request_id: str, share_url: Optional[str] = None) -> tuple[bytes, str]:
    """The final document once Done: the internal work report, or the vendor's handover record."""
    vendor = de.get_row(request_id).get("mode") == "VENDOR"
    return build(request_id, share_url, variant="vendor" if vendor else "internal")


def build(request_id: str, share_url: Optional[str] = None, public: bool = False, variant: str = "report") -> tuple[bytes, str]:
    """public = the share link's copy: without the invoices (cost documents stay with the team and the PIC).
    variant: report (the request at any time), internal (final work report), vendor (final handover record)."""
    r = de.full(request_id)
    if public:
        r = de.without_invoices(r)
    doc_title = {"report": "Laporan Pengajuan", "internal": "Laporan Pekerjaan Maintenance",
                 "vendor": "Berita Acara Penyelesaian Pekerjaan Vendor"}[variant]
    vendors = sorted({a.get("company") or a["name"] for a in r["assignments"] if a["kind"] == "VENDOR" and a["status"] != "RELEASED"})
    s = _S()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=30 * mm, bottomMargin=18 * mm,
                            title=f"{r['number']} - Damaged Equipment", author="Kopi Calf Integrated Portal")
    story: list = []

    # title: kind + number, status and urgency pills
    story.append(Table([[[Paragraph(f"<font color='{HEX['brand']}'><b>{_esc(doc_title).upper()}</b></font>", s.label),
                          Paragraph(_esc(r["kindLabel"]), s.title),
                          Paragraph(f"{_esc(r['branchName'])} ({_esc(r['branchCode'])}) · PIC {_esc(r['picName'])}", s.small)],
                         [_pill(r["statusLabel"], STATUS_PILL.get(r["status"], ("#f1f5f9", "#334155")), s, 34 * mm), Spacer(1, 3),
                          _pill(f"Urgensi {r['urgencyLabel']}", URGENCY_PILL.get(r["urgency"], ("#f1f5f9", "#334155")), s, 34 * mm)]]],
                       colWidths=[WIDTH - 36 * mm, 36 * mm],
                       style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)])))
    story.append(Spacer(1, 8))
    story.append(_steps(r, s))
    if r["status"] in ("ON_HOLD", "REJECTED"):
        why = next((e["note"] for e in reversed(r["events"]) if e["to"] == r["status"] and e["note"]), None)
        tone = STATUS_PILL[r["status"]]
        story.append(Spacer(1, 4))
        story.append(Table([[Paragraph(f"<font color='{tone[1]}'><b>{'On hold' if r['status'] == 'ON_HOLD' else 'Ditolak'}</b>"
                                       f"{' — ' + _esc(why) if why else ''}</font>", s.body)]], colWidths=[WIDTH],
                           style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(tone[0])), ("ROUNDEDCORNERS", [4, 4, 4, 4]),
                                             ("LEFTPADDING", (0, 0), (-1, -1), 8)])))

    t = r["tracker"]
    n = 0

    def sec(title: str, hint: str = "") -> Table:
        nonlocal n
        n += 1
        return _section(n, title, s, hint)

    story.append(sec("Data pengajuan"))
    facts = [("Nomor", r["number"]), ("Tanggal pengajuan", _wib(r["submittedAt"])), ("Cabang", r["branchName"]), ("PIC", r["picName"]),
             ("Jenis", r["kindLabel"]), ("Urgensi", r["urgencyLabel"]),
             ("Lokasi saat kirim", "-" if r["distanceM"] is None else f"{r['distanceM']} m dari outlet" + (" (di luar radius)" if r["outsideRadius"] else "")),
             ("Waktu proses", f"{_hours(t['workHours'])}" + (f" / target {_hours(t['slaHours'])}" if t["slaHours"] else "")),
             ("Target selesai", _wib(t.get("dueAt")) + (" (disesuaikan tim)" if t.get("targetAdjusted") else "")),
             ("Penanganan", "Vendor" if r.get("mode") == "VENDOR" else "Internal" if r.get("mode") == "INTERNAL" else "-")]
    if r["kind"] == "REPAIR" and r["cause"]:
        facts.append(("Penyebab", CAUSE.get(r["cause"], r["cause"])))
    if r["closedAt"]:
        facts.append(("Ditutup", _wib(r["closedAt"])))
    if vendors:
        facts.append(("Vendor", ", ".join(vendors)))
    story.append(_grid(facts, s))

    story.append(sec("Daftar item", f"{len(r['items'])} item"))
    rows = [[Paragraph(str(i), s.body), Paragraph(f"<b>{_esc(it['name'])}</b><br/><font size=7 color='{HEX['muted']}'>{_esc(it['category'])} · {_esc(it['group'])}</font>", s.body),
             Paragraph(str(it["qty"]), s.body), Paragraph(_esc(it["location"] or "-"), s.body), Paragraph(de.URGENCY_LABEL[it["urgency"]], s.body),
             Paragraph(_esc(it["note"] or "-"), s.body)] for i, it in enumerate(r["items"], start=1)]
    story.append(_table(["No", "Item & kategori", "Qty", "Lokasi", "Urgensi", "Keterangan"], rows, [0.05, 0.33, 0.06, 0.14, 0.11, 0.31], s))

    story.append(sec("Analisis kerusakan" if r["kind"] == "REPAIR" else "Alasan pengadaan"))
    story.append(Table([[Paragraph(_esc(r["analysis"] or "-"), s.body)]], colWidths=[WIDTH],
                       style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), SOFT), ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                                         ("LEFTPADDING", (0, 0), (-1, -1), 9), ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)])))

    before = [m for m in r["media"] if m["kind"] == "PHOTO"]
    after = [m for m in r["media"] if m["kind"] == "AFTER"]
    watch = [m for m in r["media"] if m["kind"] == "MONITOR"]
    handover_photos = [m for m in r["media"] if m["kind"] == "HANDOVER"]
    ho = r.get("handover") or {}
    video = next((m for m in r["media"] if m["kind"] == "VIDEO"), None)
    story.append(sec("Evidence dari outlet", f"{len(before)} foto" + (" · 1 video" if video else "")))
    grid = _photos(before, s, lambda m: f"Diambil {_wib(m['takenAt'] or m['uploadedAt'])}")
    story.append(grid or Paragraph("Tidak ada foto.", s.small))
    if video:
        story.append(Paragraph(f"Video: {_esc(video['fileName'] or 'video')} ({video['bytes'] / 1048576:.1f} MB)"
                               + (" — putar dari link pengajuan." if share_url else "."), s.small))

    active = [a for a in r["assignments"] if a["status"] in ("ACTIVE", "DONE")]
    if active or after or r["estCost"] is not None or r["actualCost"] is not None:
        story.append(sec("Biaya pekerjaan vendor" if variant == "vendor" else "Penanganan",
                         "vendor, biaya dan invoice" if variant == "vendor" else "tukang, biaya dan hasil pekerjaan"))
        if active:
            story.append(_table(["Tukang", "Jenis", "Jadwal", "Status"],
                                [[Paragraph(f"<b>{_esc(a['name'])}</b>", s.body), Paragraph("Vendor" + (f" · {_esc(a['company'])}" if a.get("company") else "")
                                                                                             if a["kind"] == "VENDOR" else "Internal", s.body),
                                  Paragraph(_esc(a.get("scheduledFor") or "-"), s.body), Paragraph("Selesai" if a["status"] == "DONE" else "Aktif", s.body)]
                                 for a in active], [0.38, 0.3, 0.16, 0.16], s))
            story.append(Spacer(1, 6))
        lines = [] if public else de.cost_items(request_id)
        work = [] if public else [x for x in de.labor(request_id) if x["amount"]]
        if lines or work:
            kinds = {"MATERIAL": "Material", "SERVICE": "Jasa", "TRANSPORT": "Transport", "OTHER": "Lainnya"}
            rows = [[Paragraph(kinds.get(i["kind"], i["kind"]), s.small), Paragraph(f"<b>{_esc(i['description'])}</b>", s.body),
                     Paragraph(f"{i['qty']:g}", s.body), Paragraph(_rp(i["estAmount"]), s.right), Paragraph(_rp(i["actualAmount"]), s.right)]
                    for i in lines]
            rows += [[Paragraph("Upah", s.small),
                      Paragraph(f"<b>{_esc(x['technician'])}</b><br/><font size=7 color='{HEX['muted']}'>"
                                + (f"{x['units']:g} {x['unit']} × {_rp(x['rate'])}" + (" (÷26 per hari)" if x["wageType"] == "MONTHLY" else "")
                                   if x["adjust"] is None else f"disesuaikan: {_esc(x['note'] or '-')}") + "</font>", s.body),
                      Paragraph("-", s.body), Paragraph("-", s.right), Paragraph(_rp(x["amount"]), s.right)] for x in work]
            rows.append([Paragraph("", s.body), Paragraph("<b>Total</b>", s.body), Paragraph("", s.body),
                         Paragraph(f"<b>{_rp(r['estCost'])}</b>", s.right), Paragraph(f"<b>{_rp(r['actualCost'])}</b>", s.right)])
            story.append(_table(["Jenis", "Deskripsi", "Qty", "Estimasi", "Aktual"], rows, [0.12, 0.46, 0.08, 0.17, 0.17], s))
            story.append(Spacer(1, 6))
        elif r["estCost"] is not None or r["actualCost"] is not None:
            story.append(_grid([("Estimasi biaya", _rp(r["estCost"])), ("Biaya aktual", _rp(r["actualCost"])),
                                ("Selisih", _rp((r["actualCost"] or 0) - (r["estCost"] or 0)) if r["actualCost"] is not None and r["estCost"] is not None else "-")], s, cols=3))
            story.append(Spacer(1, 6))
        invoices = [m for m in r["media"] if m["kind"] == "INVOICE"]
        if invoices:
            story.append(_table(["Invoice / bukti biaya", "Catatan", "Diunggah"],
                                [[Paragraph(_esc(m["fileName"] or "invoice"), s.body), Paragraph(_esc(m.get("note") or "-"), s.body),
                                  Paragraph(f"{_wib(m['uploadedAt'])}<br/>{_esc(m['uploadedBy'] or '-')}", s.small)] for m in invoices],
                                [0.36, 0.38, 0.26], s))
            story.append(Spacer(1, 6))
        if after and variant != "vendor":
            story.append(Paragraph("<b>Foto hasil pekerjaan</b>", s.body))
            story.append(Spacer(1, 3))
            story.append(_photos(after, s, lambda m: f"{_esc(m['uploadedBy'] or '-')} · {_wib(m['takenAt'] or m['uploadedAt'])}")
                         or Paragraph("-", s.small))

    if watch and variant != "vendor":
        story.append(sec("Foto pemantauan PIC", f"{len(watch)} foto selama pekerjaan berjalan"))
        story.append(_photos(watch, s, lambda m: _wib(m["takenAt"] or m["uploadedAt"])) or Paragraph("-", s.small))
    if ho.get("at") or handover_photos:
        loc = "-" if ho.get("distanceM") is None else f"{ho['distanceM']} m dari outlet" + (" (di luar radius)" if ho.get("outsideRadius") else "")
        story.append(sec("Serah terima di outlet", f"{_wib(ho.get('at'))} · {loc}"))
        story.append(_photos(handover_photos, s, lambda m: f"{_esc(m['uploadedBy'] or '-')} · {_wib(m['takenAt'] or m['uploadedAt'])}") or Paragraph("-", s.small))
        if ho.get("note"):
            story.append(Paragraph(f"Catatan PIC: {_esc(ho['note'])}", s.body))

    sig = de.signature_of(request_id)
    boxes = []
    bw = (WIDTH - 8) / 3
    if variant == "vendor" or ho.get("hasVendorSign"):
        v = de.handover_signature(request_id, "vendor")
        boxes.append(_sign_box(f"Vendor · {', '.join(vendors)}" if vendors else "Vendor", bytes(v["signature"]) if v else None, ho.get("vendorName") or "", _wib(ho.get("at")), s, bw))
    if ho.get("hasPicSign"):
        pc = de.handover_signature(request_id, "pic")
        boxes.append(_sign_box("PIC outlet (serah terima)", bytes(pc["signature"]) if pc else None, ho.get("picName") or "", _wib(ho.get("at")), s, bw))
    elif sig and r["signature"]:
        boxes.append(_sign_box("PIC (pengajuan)", bytes(sig["signature"]), r["signature"]["name"], _wib(r["signature"]["signedAt"]), s, bw))
    if variant != "report" or ho.get("approvedBy"):
        boxes.append(_sign_box("Disetujui tim maintenance", None, ho.get("approvedBy") or "", f"Done {_wib(r['closedAt'])}", s, bw))
    if boxes:
        title = "Tanda tangan" if variant != "report" else "Tanda tangan & persetujuan"
        story.append(KeepTogether([sec(title, "dengan tanda tangan ini para pihak menyatakan pekerjaan telah diterima"),
                                   Table([boxes + [""] * (3 - len(boxes))], colWidths=[bw + 4] * 3,
                                         style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))]))
    if variant == "report" and sig and r["signature"] and ho.get("hasPicSign"):
        story.append(Spacer(1, 4))
        story.append(Paragraph(f"Pengajuan ditandatangani PIC {_esc(r['signature']['name'])} · {_wib(r['signature']['signedAt'])}", s.small))

    if variant == "vendor":
        story.append(Spacer(1, 10))
        story.append(Paragraph("Berita acara ini dibuat dari Kopi Calf Integrated Portal setelah pekerjaan vendor dinyatakan selesai (Done) "
                               "oleh tim maintenance. Rincian riwayat penanganan internal tidak disertakan.", s.small))
        return _finish(doc, story, r, s, public, doc_title, share_url=None)

    story.append(sec("Riwayat penanganan", "setiap tahap dan catatan tim"))
    story.append(_table(["Waktu", "Proses", "Oleh", "Catatan"],
                        [[Paragraph(_wib(e["at"]), s.small), Paragraph(_esc(_event_text(e)), s.body), Paragraph(_esc(e["actor"] or "-"), s.body),
                          Paragraph(_esc(e["note"] or "-"), s.body)] for e in r["events"]], [0.18, 0.32, 0.18, 0.32], s))
    if t["totalHours"] is not None and t["stages"] and variant != "vendor":
        story.append(Spacer(1, 6))
        cells = [(st["label"], _hours(st["hours"])) for st in t["stages"]]
        cells += [("Total (tanpa on hold)", _hours(t["workHours"]))]
        if t["slaHours"]:
            cells.append(("Target", _hours(t["slaHours"]) + (" · terlewati" if t["overdue"] else "")))
        story.append(_grid(cells, s, cols=4))

    if share_url:
        story.append(Spacer(1, 10))
        link = Paragraph(f"<b>Pantau progres terbaru</b><br/>Pindai QR atau buka tautan berikut — status, catatan tim dan foto selalu "
                         f"diperbarui:<br/><link href='{share_url}' color='{HEX['brand']}'>{_esc(share_url)}</link>", s.small)
        story.append(KeepTogether(Table([[_qr(share_url, 24 * mm), link]], colWidths=[28 * mm, WIDTH - 28 * mm],
                                        style=TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                                                          ("BACKGROUND", (0, 0), (-1, -1), SOFT), ("LEFTPADDING", (0, 0), (-1, -1), 6)]))))

    return _finish(doc, story, r, s, public, doc_title, share_url)


def _finish(doc, story, r: dict, s: _S, public: bool, doc_title: str, share_url: Optional[str]) -> tuple[bytes, str]:
    printed = datetime.now(WIB).strftime("%d/%m/%Y %H:%M")

    def page(canvas, d):
        canvas.saveState()
        w, h = A4
        if LOGO.is_file():  # the Kopi Calf logo (blue on white)
            canvas.drawImage(str(LOGO), MARGIN, h - 19 * mm, width=26 * mm, height=26 * mm * 414 / 782, mask="auto")
        canvas.setFillColor(INK)
        canvas.setFont("Helvetica-Bold", 10)
        canvas.drawString(MARGIN + 30 * mm, h - 11.5 * mm, doc_title)
        canvas.setFillColor(MUTED)
        canvas.setFont("Helvetica", 7.5)
        canvas.drawString(MARGIN + 30 * mm, h - 15.5 * mm, "Damaged Equipment · Maintenance & Pengadaan")
        canvas.setFillColor(INK)
        canvas.setFont("Helvetica-Bold", 12)
        canvas.drawRightString(w - MARGIN, h - 11.5 * mm, r["number"] or "")
        canvas.setFillColor(MUTED)
        canvas.setFont("Helvetica", 7.5)
        canvas.drawRightString(w - MARGIN, h - 15.5 * mm, f"Dikirim {_wib(r['submittedAt'])}")
        canvas.setFillColor(BRAND)
        canvas.rect(MARGIN, h - 22 * mm, w - 2 * MARGIN, 0.8 * mm, stroke=0, fill=1)
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.6)
        canvas.line(MARGIN, 12 * mm, w - MARGIN, 12 * mm)
        canvas.setFillColor(MUTED)
        canvas.setFont("Helvetica", 7)
        canvas.drawString(MARGIN, 8 * mm, f"{r['number']} · {doc_title} · dicetak {printed} WIB · Kopi Calf Integrated Portal"
                          + (" · salinan link share" if public else ""))
        canvas.drawRightString(w - MARGIN, 8 * mm, f"Halaman {d.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=page, onLaterPages=page)
    suffix = {"Laporan Pengajuan": "", "Laporan Pekerjaan Maintenance": "-laporan-pekerjaan"}.get(doc_title, "-berita-acara-vendor")
    return doc.filename.getvalue(), f"{r['number']}{suffix}.pdf"

