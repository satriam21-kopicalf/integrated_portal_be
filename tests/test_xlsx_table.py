"""POST /api/xlsx (app/routes/xlsx.py): the table of a page as an Excel file."""
import io
import zipfile

from app.routes.xlsx import TableIn, build


def test_table_becomes_an_excel_file():
    data = build(TableIn(fileName="x", sheet="Pengajuan", title="Monitoring", header=["No", "Outlet", "Total"],
                         rows=[[1, "Kemang", 125000], [2, "Cipete", None]]))
    z = zipfile.ZipFile(io.BytesIO(data))
    sheet = z.read("xl/worksheets/sheet1.xml").decode()
    assert "Kemang" in sheet and "125000" in sheet and "Monitoring" in sheet and "Diunduh" in sheet
