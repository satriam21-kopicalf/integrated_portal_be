"""The document of a Damaged Equipment request (app/damaged_equipment.py) as a PDF: header, request data, items,
analysis, evidence photos, the PIC's signature, the status history with notes and the time per stage."""
import io
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app import damaged_equipment as de

WIB = ZoneInfo("Asia/Jakarta")
INK, MUTED, LINE, BRAND = colors.HexColor("#0f172a"), colors.HexColor("#64748b"), colors.HexColor("#e2e8f0"), colors.HexColor("#1d4ed8")
CAUSE = {"WEAR": "Pemakaian normal", "MISUSE": "Salah pemakaian", "AGE": "Umur barang", "OTHER": "Lainnya"}
EVENT = {"SUBMIT": "Dikirim", "ASSIGN": "Tukang ditugaskan", "RELEASE": "Tukang dilepas", "SWAP": "Pergeseran tukang",
         "COST": "Biaya diperbarui", "NOTE": "Catatan", "DUPLICATE": "Ditandai duplikat"}


def _wib(v: Optional[str]) -> str:
    if not v:
        return "-"
    return datetime.fromisoformat(v).astimezone(WIB).strftime("%d/%m/%Y %H:%M") + " WIB"


def _hours(h: Optional[float]) -> str:
    if h is None:
        return "-"
    return f"{h:.1f} jam" if h < 48 else f"{h / 24:.1f} hari"


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


def build(request_id: str, share_url: Optional[str] = None) -> tuple[bytes, str]:
    r = de.full(request_id)
    styles = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=styles["Normal"], fontName="Helvetica", fontSize=9, leading=12, textColor=INK)
    small = ParagraphStyle("s", parent=body, fontSize=7.5, leading=10, textColor=MUTED)
    head = ParagraphStyle("h", parent=body, fontName="Helvetica-Bold", fontSize=11, leading=14, spaceBefore=8, spaceAfter=4)
    title = ParagraphStyle("t", parent=body, fontName="Helvetica-Bold", fontSize=15, leading=19)
    right = ParagraphStyle("r", parent=body, alignment=TA_RIGHT)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
                            title=f"{r['number']} - Damaged Equipment", author="Kopi Calf Integrated Portal")
    width = A4[0] - 32 * mm
    story = []

    story.append(Table([[Paragraph("KOPI CALF · DAMAGED EQUIPMENT", ParagraphStyle("k", parent=small, textColor=BRAND, fontName="Helvetica-Bold")),
                         Paragraph(f"<b>{_esc(r['number'])}</b>", right)],
                        [Paragraph(f"Pengajuan {_esc(r['kindLabel'])}", title), Paragraph(f"Status: <b>{_esc(r['statusLabel'])}</b>", right)]],
                       colWidths=[width * 0.68, width * 0.32], style=TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                                                                                 ("LINEBELOW", (0, 1), (-1, 1), 1.2, BRAND),
                                                                                 ("BOTTOMPADDING", (0, 1), (-1, 1), 6)])))
    story.append(Spacer(1, 6))
    info = [
        ["Tanggal pengajuan", _wib(r["submittedAt"]), "Cabang", f"{r['branchName']} ({r['branchCode']})"],
        ["PIC", r["picName"], "Urgensi", r["urgencyLabel"]],
        ["Jenis", r["kindLabel"], "Lokasi saat kirim",
         "-" if r["distanceM"] is None else f"{r['distanceM']} m dari outlet{' (di luar radius)' if r['outsideRadius'] else ''}"],
    ]
    if r["kind"] == "REPAIR" and r["cause"]:
        info.append(["Penyebab", CAUSE.get(r["cause"], r["cause"]), "", ""])
    if r["estCost"] is not None or r["actualCost"] is not None or r["vendor"]:
        rp = lambda v: "-" if v is None else f"Rp {v:,.0f}".replace(",", ".")  # noqa: E731
        info.append(["Estimasi biaya", rp(r["estCost"]), "Biaya aktual", rp(r["actualCost"])])
        info.append(["Vendor", r["vendor"] or "-", "", ""])
    story.append(Table([[Paragraph(f"<font color='#64748b'>{_esc(a)}</font>", small), Paragraph(_esc(b), body),
                         Paragraph(f"<font color='#64748b'>{_esc(c)}</font>", small), Paragraph(_esc(d), body)] for a, b, c, d in info],
                       colWidths=[width * 0.16, width * 0.34, width * 0.16, width * 0.34],
                       style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 2)])))

    story.append(Paragraph("Daftar item", head))
    rows = [[Paragraph(f"<b>{h}</b>", small) for h in ("No", "Kategori", "Item", "Qty", "Lokasi", "Urgensi", "Keterangan")]]
    for n, it in enumerate(r["items"], start=1):
        rows.append([Paragraph(str(n), body), Paragraph(f"{_esc(it['category'])}<br/><font size=7 color='#64748b'>{_esc(it['group'])}</font>", body),
                     Paragraph(_esc(it["name"]), body), Paragraph(str(it["qty"]), body), Paragraph(_esc(it["location"] or "-"), body),
                     Paragraph(de.URGENCY_LABEL[it["urgency"]], body), Paragraph(_esc(it["note"] or "-"), body)])
    story.append(Table(rows, colWidths=[width * w for w in (0.05, 0.19, 0.2, 0.06, 0.14, 0.11, 0.25)], repeatRows=1,
                       style=TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")), ("GRID", (0, 0), (-1, -1), 0.4, LINE),
                                         ("VALIGN", (0, 0), (-1, -1), "TOP")])))

    story.append(Paragraph("Analisis kerusakan" if r["kind"] == "REPAIR" else "Alasan pengadaan", head))
    story.append(Paragraph(_esc(r["analysis"] or "-"), body))

    photos = [m for m in r["media"] if m["kind"] in ("PHOTO", "AFTER")]
    if photos:
        story.append(Paragraph("Evidence foto", head))
        cells = []
        for m in photos:
            f = de.media_file(m["id"])
            img = _image(f["path"], width / 3 - 6, 55 * mm) if f and f["path"] else None
            if img:
                cap = ("Hasil pekerjaan · " if m["kind"] == "AFTER" else "") + _wib(m["takenAt"] or m["uploadedAt"])
                cells.append([img, Paragraph(cap, small)])
        grid = [cells[i:i + 3] for i in range(0, len(cells), 3)]
        if grid:
            grid[-1] += [""] * (3 - len(grid[-1]))
            story.append(Table([[Table([[c[0]], [c[1]]]) if c else "" for c in row] for row in grid], colWidths=[width / 3] * 3,
                               style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")])))
    invoices = [m for m in r["media"] if m["kind"] == "INVOICE"]
    if invoices:
        story.append(Paragraph("Invoice / bukti biaya", head))
        story.append(Paragraph("<br/>".join(f"{_esc(m['fileName'] or 'invoice')}{' · ' + _esc(m['note']) if m.get('note') else ''} · "
                                            f"{_wib(m['uploadedAt'])} · {_esc(m['uploadedBy'] or '-')}" for m in invoices), small))
    videos = [m for m in r["media"] if m["kind"] == "VIDEO"]
    if videos:
        story.append(Paragraph(f"Video: {_esc(videos[0]['fileName'] or 'video')} ({videos[0]['bytes'] / 1048576:.1f} MB)"
                               + (" — putar dari link pengajuan." if share_url else "."), small))

    sig = de.signature_of(request_id)
    if sig and r["signature"]:
        story.append(Paragraph("Tanda tangan PIC", head))
        reader_img = None
        try:
            reader_img = Image(io.BytesIO(bytes(sig["signature"])), width=50 * mm, height=22 * mm, kind="proportional")
            reader_img.hAlign = "LEFT"
        except Exception:  # noqa: BLE001
            reader_img = None
        story.append(KeepTogether([reader_img or Spacer(1, 1),
                                   Paragraph(f"<b>{_esc(r['signature']['name'])}</b> · {_wib(r['signature']['signedAt'])}", body)]))

    story.append(Paragraph("Riwayat penanganan", head))
    ev_rows = [[Paragraph(f"<b>{h}</b>", small) for h in ("Waktu", "Proses", "Oleh", "Catatan")]]
    for e in r["events"]:
        what = (f"{de.STATUS_LABEL.get(e['from'] or '', e['from'] or '')} → {de.STATUS_LABEL.get(e['to'] or '', e['to'] or '')}"
                if e["kind"] == "STATUS" else EVENT.get(e["kind"], e["kind"]))
        extra = e["details"].get("technician")
        if extra:
            what += f" · {extra}"
        ev_rows.append([Paragraph(_wib(e["at"]), small), Paragraph(_esc(what), body), Paragraph(_esc(e["actor"] or "-"), body),
                        Paragraph(_esc(e["note"] or "-"), body)])
    story.append(Table(ev_rows, colWidths=[width * w for w in (0.2, 0.28, 0.18, 0.34)], repeatRows=1,
                       style=TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")), ("GRID", (0, 0), (-1, -1), 0.4, LINE),
                                         ("VALIGN", (0, 0), (-1, -1), "TOP")])))
    t = r["tracker"]
    if t["totalHours"] is not None:
        stages = " · ".join(f"{s['label']} {_hours(s['hours'])}" for s in t["stages"])
        sla = f" · target {_hours(t['slaHours'])}{' (terlewati)' if t['overdue'] else ''}" if t["slaHours"] else ""
        story.append(Spacer(1, 4))
        story.append(Paragraph(f"Waktu proses: {_hours(t['totalHours'])} (on hold {_hours(t['holdHours'])}){sla}<br/>{_esc(stages)}", small))
    if share_url:
        story.append(Spacer(1, 8))
        story.append(Paragraph(f"Lihat pengajuan & progres terbaru: <link href='{share_url}' color='#1d4ed8'>{_esc(share_url)}</link>", small))

    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(16 * mm, 8 * mm, f"{r['number']} · dibuat {datetime.now(WIB).strftime('%d/%m/%Y %H:%M')} WIB · Kopi Calf Integrated Portal")
        canvas.drawRightString(A4[0] - 16 * mm, 8 * mm, f"Halaman {d.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue(), f"{r['number']}.pdf"
