import openpyxl

from app.xlsx_stream import StreamingXlsxWriter


def test_streaming_writer_produces_valid_workbook(tmp_path):
    path = tmp_path / "out.xlsx"
    with StreamingXlsxWriter(str(path)) as xlsx:
        sheet = xlsx.add_sheet("Data", widths=[12, 30, 10], header=["Name", "Notes", "Qty"])
        sheet.write_rows([
            ["A & B", "<tag> \x0b ctrl", 2],
            ["  padded ", "", 2.5],
            [None, "x" * 40000, True],
        ])
        xlsx.add_static_sheet("Summary", [["Report"], [], ["Rows", 3]], bold_rows=(0,), first=True)

    wb = openpyxl.load_workbook(path)  # full parse, not read-only
    assert wb.sheetnames == ["Summary", "Data"]
    ws = wb["Data"]
    assert ws.freeze_panes == "A2"
    assert ws["A1"].font.b and not ws["A2"].font.b
    assert ws.column_dimensions["B"].width == 30
    assert ws["A2"].value == "A & B"
    assert ws["B2"].value == "<tag>  ctrl"  # illegal control char removed
    assert ws["C2"].value == 2
    assert ws["A3"].value == "  padded "  # whitespace preserved
    assert ws["B3"].value is None and ws["C3"].value == 2.5
    assert ws["A4"].value is None
    assert len(ws["B4"].value) == 32767  # Excel cell limit
    assert ws["C4"].value is True
    assert wb["Summary"]["A1"].font.b and wb["Summary"]["B3"].value == 3
