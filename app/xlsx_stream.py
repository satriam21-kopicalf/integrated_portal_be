"""Minimal streaming XLSX writer for very large exports.

xlsxwriter manages ~2-3k rows/s for our 44-column rows, which turns a monthly
export (~2M rows) into a 15+ minute job. This writer emits the worksheet XML
directly into the zip archive in batches (inline strings, no shared-string
table), which is an order of magnitude faster. It supports exactly what the
ESB report needs: multiple sheets, a bold header row, column widths and a
frozen header.

Usage:
    with StreamingXlsxWriter(path) as xlsx:
        sheet = xlsx.add_sheet("Transactions", widths=[20, 15], header=["A", "B"])
        sheet.write_rows(rows)            # iterable of lists
        xlsx.add_static_sheet("Summary", rows, first=True)
"""
import zipfile
from typing import Any, Iterable, Optional, Sequence

MAX_STRING_LEN = 32767  # Excel cell limit

# XML escaping plus removal of control characters that are illegal in XML 1.0
_ESCAPE = {ord("&"): "&amp;", ord("<"): "&lt;", ord(">"): "&gt;"}
_ESCAPE.update({c: None for c in range(32) if c not in (9, 10, 13)})


def _col_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


_COLS = [_col_letter(i) for i in range(16384)]


def _escape(text: str) -> str:
    return text.translate(_ESCAPE)


def _row_xml(row_num: int, values: Sequence[Any], style: int = 0) -> str:
    s = f' s="{style}"' if style else ""
    cells = []
    for col, value in enumerate(values):
        if value is None or value == "":
            continue
        ref = f"{_COLS[col]}{row_num}"
        if isinstance(value, bool):
            cells.append(f'<c r="{ref}"{s} t="b"><v>{int(value)}</v></c>')
        elif isinstance(value, (int, float)):
            cells.append(f'<c r="{ref}"{s}><v>{value!r}</v></c>')
        else:
            text = _escape(str(value)[:MAX_STRING_LEN])
            space = ' xml:space="preserve"' if text[:1].isspace() or text[-1:].isspace() else ""
            cells.append(f'<c r="{ref}"{s} t="inlineStr"><is><t{space}>{text}</t></is></c>')
    return f'<row r="{row_num}">{"".join(cells)}</row>'


_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
_NS_R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'

_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f"<styleSheet {_NS}>"
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/><family val="2"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)
BOLD = 1


class Sheet:
    def __init__(self, stream, widths: Optional[Sequence[float]], freeze_header: bool):
        self._stream = stream
        self.rows = 0
        parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', f"<worksheet {_NS} {_NS_R}>"]
        if freeze_header:
            parts.append(
                '<sheetViews><sheetView workbookViewId="0">'
                '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
                '<selection pane="bottomLeft"/></sheetView></sheetViews>'
            )
        else:
            parts.append('<sheetViews><sheetView workbookViewId="0"/></sheetViews>')
        if widths:
            cols = "".join(
                f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths)
            )
            parts.append(f"<cols>{cols}</cols>")
        parts.append("<sheetData>")
        self._write("".join(parts))

    def _write(self, text: str) -> None:
        self._stream.write(text.encode("utf-8"))

    def write_row(self, values: Sequence[Any], style: int = 0) -> None:
        self.rows += 1
        self._write(_row_xml(self.rows, values, style))

    def write_rows(self, rows: Iterable[Sequence[Any]]) -> None:
        start = self.rows
        chunk = [_row_xml(start + i, values) for i, values in enumerate(rows, start=1)]
        self.rows = start + len(chunk)
        self._write("".join(chunk))

    def close(self) -> None:
        self._write("</sheetData></worksheet>")
        self._stream.close()


class StreamingXlsxWriter:
    def __init__(self, path: str, compresslevel: int = 6):
        self._zip = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=compresslevel, allowZip64=True)
        self._sheets: list[tuple[str, int, bool]] = []  # (name, part number, first)
        self._current: Optional[Sheet] = None

    def __enter__(self) -> "StreamingXlsxWriter":
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.close()
        else:
            self._zip.close()

    def _open_part(self, name: str, first: bool) -> Any:
        if self._current is not None:
            self._current.close()
            self._current = None
        number = len(self._sheets) + 1
        self._sheets.append((name[:31], number, first))
        return self._zip.open(f"xl/worksheets/sheet{number}.xml", "w", force_zip64=True)

    def add_sheet(self, name: str, widths: Optional[Sequence[float]] = None,
                  header: Optional[Sequence[str]] = None) -> Sheet:
        sheet = Sheet(self._open_part(name, False), widths, freeze_header=header is not None)
        if header is not None:
            sheet.write_row(header, BOLD)
        self._current = sheet
        return sheet

    def add_static_sheet(self, name: str, rows: Sequence[Sequence[Any]], widths: Optional[Sequence[float]] = None,
                         bold_rows: Sequence[int] = (), first: bool = False) -> None:
        sheet = Sheet(self._open_part(name, first), widths, freeze_header=False)
        for i, values in enumerate(rows):
            sheet.write_row(values, BOLD if i in bold_rows else 0)
        sheet.close()

    def close(self) -> None:
        if self._current is not None:
            self._current.close()
            self._current = None
        ordered = [s for s in self._sheets if s[2]] + [s for s in self._sheets if not s[2]]
        sheets_xml = "".join(
            f'<sheet name="{_escape(name)}" sheetId="{i}" r:id="rId{number}"/>'
            for i, (name, number, _) in enumerate(ordered, start=1)
        )
        style_rid = len(self._sheets) + 1
        rels = "".join(
            f'<Relationship Id="rId{number}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{number}.xml"/>'
            for _, number, _ in self._sheets
        )
        overrides = "".join(
            f'<Override PartName="/xl/worksheets/sheet{number}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for _, number, _ in self._sheets
        )
        header = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        files = {
            "[Content_Types].xml": (
                f'{header}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/xl/workbook.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/styles.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                f"{overrides}</Types>"
            ),
            "_rels/.rels": (
                f'{header}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                'Target="xl/workbook.xml"/></Relationships>'
            ),
            "xl/workbook.xml": (
                f"{header}<workbook {_NS} {_NS_R}><bookViews><workbookView/></bookViews>"
                f"<sheets>{sheets_xml}</sheets></workbook>"
            ),
            "xl/_rels/workbook.xml.rels": (
                f'{header}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f"{rels}"
                f'<Relationship Id="rId{style_rid}" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
                'Target="styles.xml"/></Relationships>'
            ),
            "xl/styles.xml": _STYLES,
        }
        for name, content in files.items():
            self._zip.writestr(name, content)
        self._zip.close()
