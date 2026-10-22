"""The document of a Damaged Equipment request (app/damaged_equipment.py) as a PDF report: a brand band, the title with
status and urgency, the steps, the request data, items, analysis, photos (damage / work done), cost & invoices (team copy),
the PIC's signature, the handling history with notes, the time per stage and a QR code to the live share link."""
import io
from datetime import datetime
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
         "COST": "Biaya diperbarui", "NOTE": "Catatan tim", "DUPLICATE": "Ditandai duplikat", "WORK": "Link foto tukang"}
# (background, text) of the status / urgency pills
STATUS_PILL = {"PENDING": ("#f1f5f9", "#334155"), "VALIDATION": ("#e0f2fe", "#075985"), "REVIEW": ("#ede9fe", "#5b21b6"),
               "ON_HOLD": ("#fef3c7", "#92400e"), "ACCEPTED": ("#dbeafe", "#1e40af"), "IN_PROGRESS": ("#dbeafe", "#1e3a8a"),
               "DONE": ("#dcfce7", "#166534"), "REJECTED": ("#fee2e2", "#991b1b")}
URGENCY_PILL = {"CRITICAL": ("#fee2e2", "#991b1b"), "HIGH": ("#ffedd5", "#9a3412"), "NORMAL": ("#dbeafe", "#1e40af"),
                "LOW": ("#f1f5f9", "#334155"), "PLANNED": ("#ede9fe", "#5b21b6")}
STEPS = ["PENDING", "VALIDATION", "REVIEW", "ACCEPTED", "IN_PROGRESS", "DONE"]
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
    return Table([[chip, text]], colWidths=[9 * mm, WIDTH - 9 * mm],
                 style=TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LINEBELOW", (0, 0), (-1, -1), 0.6, LINE),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 10)]))


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
    if e["kind"] == "COST":
        return f"Biaya diperbarui · estimasi {_rp(d.get('estCost'))}, aktual {_rp(d.get('actualCost'))}"
    if e["kind"] == "WORK":
        return (f"Foto pengerjaan dikirim tukang · {d.get('photos', 0)} foto" if d.get("link") == "sent"
                else f"Link foto dikirim ke tukang · {d.get('technician', '')}")
    if e["kind"] == "DUPLICATE":
        return f"Ditandai duplikat dari {d.get('of', '')}"
    text = EVENT.get(e["kind"], e["kind"])
    if d.get("technician"):
        text += f" · {d['technician']}"
    if e["kind"] == "SWAP" and (d.get("to") or d.get("from")):
        text += f" ({'ke ' + d['to'] if d.get('to') else 'dari ' + d['from']})"
    return text


def build(request_id: str, share_url: Optional[str] = None, public: bool = False) -> tuple[bytes, str]:
    """public = the share link's copy: without the invoices (cost documents stay with the team and the PIC)."""
    r = de.full(request_id)
    if public:
        r = de.without_invoices(r)
    s = _S()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=30 * mm, bottomMargin=18 * mm,
                            title=f"{r['number']} - Damaged Equipment", author="Kopi Calf Integrated Portal")
    story: list = []

    # title: kind + number, status and urgency pills
    story.append(Table([[[Paragraph(f"<font color='{HEX['brand']}'><b>LAPORAN PENGAJUAN</b></font>", s.label),
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
             ("Waktu proses", f"{_hours(t['workHours'])}" + (f" / target {_hours(t['slaHours'])}" if t["slaHours"] else ""))]
    if r["kind"] == "REPAIR" and r["cause"]:
        facts.append(("Penyebab", CAUSE.get(r["cause"], r["cause"])))
    if r["closedAt"]:
        facts.append(("Ditutup", _wib(r["closedAt"])))
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
    video = next((m for m in r["media"] if m["kind"] == "VIDEO"), None)
    story.append(sec("Evidence dari outlet", f"{len(before)} foto" + (" · 1 video" if video else "")))
    grid = _photos(before, s, lambda m: f"Diambil {_wib(m['takenAt'] or m['uploadedAt'])}")
    story.append(grid or Paragraph("Tidak ada foto.", s.small))
    if video:
        story.append(Paragraph(f"Video: {_esc(video['fileName'] or 'video')} ({video['bytes'] / 1048576:.1f} MB)"
                               + (" — putar dari link pengajuan." if share_url else "."), s.small))

    active = [a for a in r["assignments"] if a["status"] in ("ACTIVE", "DONE")]
    if active or after or r["estCost"] is not None or r["actualCost"] is not None or r["vendor"]:
        story.append(sec("Penanganan", "tukang, biaya dan hasil pekerjaan"))
        if active:
            story.append(_table(["Tukang", "Jenis", "Jadwal", "Status"],
                                [[Paragraph(f"<b>{_esc(a['name'])}</b>", s.body), Paragraph("Vendor" + (f" · {_esc(a['company'])}" if a.get("company") else "")
                                                                                             if a["kind"] == "VENDOR" else "Internal", s.body),
                                  Paragraph(_esc(a.get("scheduledFor") or "-"), s.body), Paragraph("Selesai" if a["status"] == "DONE" else "Aktif", s.body)]
                                 for a in active], [0.38, 0.3, 0.16, 0.16], s))
            story.append(Spacer(1, 6))
        if r["estCost"] is not None or r["actualCost"] is not None or r["vendor"]:
            story.append(_grid([("Estimasi biaya", _rp(r["estCost"])), ("Biaya aktual", _rp(r["actualCost"])), ("Vendor", r["vendor"] or "-")], s, cols=3))
            story.append(Spacer(1, 6))
        invoices = [m for m in r["media"] if m["kind"] == "INVOICE"]
        if invoices:
            story.append(_table(["Invoice / bukti biaya", "Catatan", "Diunggah"],
                                [[Paragraph(_esc(m["fileName"] or "invoice"), s.body), Paragraph(_esc(m.get("note") or "-"), s.body),
                                  Paragraph(f"{_wib(m['uploadedAt'])}<br/>{_esc(m['uploadedBy'] or '-')}", s.small)] for m in invoices],
                                [0.36, 0.38, 0.26], s))
            story.append(Spacer(1, 6))
        if after:
            story.append(Paragraph("<b>Foto hasil pekerjaan</b>", s.body))
            story.append(Spacer(1, 3))
            story.append(_photos(after, s, lambda m: f"{_esc(m['uploadedBy'] or '-')} · {_wib(m['takenAt'] or m['uploadedAt'])}")
                         or Paragraph("-", s.small))

    sig = de.signature_of(request_id)
    if sig and r["signature"]:
        try:
            img = Image(io.BytesIO(bytes(sig["signature"])), width=52 * mm, height=22 * mm, kind="proportional")
        except Exception:  # noqa: BLE001
            img = Spacer(1, 22 * mm)
        box = Table([[Paragraph("TANDA TANGAN PIC", s.label)], [img],
                     [Paragraph(f"<b>{_esc(r['signature']['name'])}</b>", s.body)], [Paragraph(_wib(r["signature"]["signedAt"]), s.small)]],
                    colWidths=[70 * mm], style=TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINE), ("LINEABOVE", (0, 2), (-1, 2), 0.6, LINE),
                                                           ("LEFTPADDING", (0, 0), (-1, -1), 8), ("ALIGN", (0, 1), (-1, 1), "LEFT")]))
        note = Paragraph("Dengan tanda tangan ini PIC menyatakan pengajuan benar dan dibutuhkan untuk operasional outlet.", s.small)
        story.append(KeepTogether([sec("Pernyataan PIC"), Table([[box, note]], colWidths=[74 * mm, WIDTH - 74 * mm],
                                                                 style=TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))]))

    story.append(sec("Riwayat penanganan", "setiap tahap dan catatan tim"))
    story.append(_table(["Waktu", "Proses", "Oleh", "Catatan"],
                        [[Paragraph(_wib(e["at"]), s.small), Paragraph(_esc(_event_text(e)), s.body), Paragraph(_esc(e["actor"] or "-"), s.body),
                          Paragraph(_esc(e["note"] or "-"), s.body)] for e in r["events"]], [0.18, 0.32, 0.18, 0.32], s))
    if t["totalHours"] is not None and t["stages"]:
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

    printed = datetime.now(WIB).strftime("%d/%m/%Y %H:%M")

    def page(canvas, d):
        canvas.saveState()
        w, h = A4
        canvas.setFillColor(NAVY)
        canvas.rect(0, h - 20 * mm, w, 20 * mm, stroke=0, fill=1)
        canvas.setFillColor(BRAND)
        canvas.rect(0, h - 21 * mm, w, 1 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 13)
        canvas.drawString(MARGIN, h - 11 * mm, "KOPI CALF")
        canvas.setFont("Helvetica", 8)
        canvas.drawString(MARGIN, h - 15.5 * mm, "Damaged Equipment · Maintenance & Pengadaan")
        canvas.setFont("Helvetica-Bold", 12)
        canvas.drawRightString(w - MARGIN, h - 11 * mm, r["number"] or "")
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(w - MARGIN, h - 15.5 * mm, f"Dikirim {_wib(r['submittedAt'])}")
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.6)
        canvas.line(MARGIN, 12 * mm, w - MARGIN, 12 * mm)
        canvas.setFillColor(MUTED)
        canvas.setFont("Helvetica", 7)
        canvas.drawString(MARGIN, 8 * mm, f"{r['number']} · dicetak {printed} WIB · Kopi Calf Integrated Portal"
                          + (" · salinan link share" if public else ""))
        canvas.drawRightString(w - MARGIN, 8 * mm, f"Halaman {d.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=page, onLaterPages=page)
    return buf.getvalue(), f"{r['number']}.pdf"
