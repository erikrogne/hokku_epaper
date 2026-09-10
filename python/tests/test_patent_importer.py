"""Focused source-ingestion and retrieval tests for iconic patents."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw

from hokku.webserver.patent_importer import (
    ImportResult,
    PatentImporter,
    RetrievalResult,
    RetrievedDrawing,
    load_source_rows,
    normalize_status,
    read_xlsx_first_sheet,
    record_from_source_row,
)
from hokku.webserver.patent_store import PatentStore


def _xlsx_fixture(path: Path) -> None:
    workbook = """<?xml version="1.0" encoding="UTF-8"?>
    <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <sheets><sheet name="Patents" sheetId="1" r:id="rId1"/></sheets>
    </workbook>"""
    relationships = """<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
        Target="worksheets/sheet1.xml"/>
    </Relationships>"""
    sheet = """<?xml version="1.0" encoding="UTF-8"?>
    <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <sheetData>
        <row r="1"><c r="A1" t="inlineStr"><is><t>Patent Number</t></is></c>
          <c r="B1" t="inlineStr"><is><t>Status</t></is></c></row>
        <row r="2"><c r="A2" t="inlineStr"><is><t>US 123</t></is></c>
          <c r="B2" t="inlineStr"><is><t>VERIFIED</t></is></c></row>
      </sheetData>
    </worksheet>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


def _png_bytes(size: tuple[int, int], box: tuple[int, int, int, int]) -> bytes:
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).rectangle(box, fill="black")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_xlsx_first_sheet_reads_inline_strings(tmp_path: Path):
    source = tmp_path / "patents.xlsx"
    _xlsx_fixture(source)

    rows = read_xlsx_first_sheet(source)

    assert rows == [(2, {"Patent Number": "US 123", "Status": "VERIFIED"})]


def test_csv_rows_and_statuses_are_normalized(tmp_path: Path):
    source = tmp_path / "patents.csv"
    source.write_text(
        "Patent Number,Status,Title\nUS 123,VERIFIED,First\nUS 456,RETRIEVE + VERIFY,Second\n",
        encoding="utf-8",
    )

    rows = load_source_rows(source)

    assert rows[0][0] == 2
    assert rows[0][1]["Title"] == "First"
    assert normalize_status("VERIFIED") == "verified"
    assert normalize_status("RETRIEVE + VERIFY") == "needs_verification"
    assert normalize_status("") == "candidate"


def test_seed_catalog_headers_map_to_display_metadata():
    record = record_from_source_row(
        2,
        {
            "Simple Name": "Sewing Machine",
            "Inventor(s)": "Elias Howe, Jr.",
            "Patent Number": "US4750A",
            "Patent Date": "1846-09-10",
            "Recommended Image": "Figure 3",
            "Display Description": "Lockstitch sewing machine using a needle and shuttle.",
            "Iconicity (1-5)": "5",
            "Visual Quality (1-5)": "5",
            "Verification Status": "VERIFIED",
            "Source URL": "https://patents.google.com/patent/US4750A/en",
        },
    )

    assert record.inventor_names == ["Elias Howe, Jr."]
    assert record.patent_year == 1846
    assert record.recommended_figure == "Figure 3"
    assert record.description == "Lockstitch sewing machine using a needle and shuttle."
    assert record.iconicity_score == 5.0
    assert record.visual_quality_score == 5.0
    assert record.qr_destination_url == record.source_url


def test_import_deduplicates_patent_numbers_and_keeps_alternates_separate(tmp_path: Path):
    source = tmp_path / "patents.csv"
    source.write_text(
        "Patent Number,Status,Title,Source URL\n"
        "US 123,VERIFIED,First,https://patents.google.com/patent/US123/en\n"
        "US123,VERIFIED,Updated,https://patents.google.com/patent/US123/en\n"
        "US 456,VERIFIED,Alternate,https://patents.google.com/patent/US456/en\n"
        ",RETRIEVE + VERIFY,Unnumbered,\n",
        encoding="utf-8",
    )

    result = PatentImporter(tmp_path, no_network=True).import_source(source)

    assert isinstance(result, ImportResult)
    records = PatentStore(tmp_path).list()
    assert [record.id for record in records] == ["US123", "US456", records[2].id]
    assert len(records) == 3
    assert records[0].patent_title == "Updated"
    assert records[2].id.startswith("candidate-")
    assert records[2].verification_status == "needs_verification"


def test_reimport_is_idempotent_and_preserves_manual_fields(tmp_path: Path):
    source = tmp_path / "patents.csv"
    source.write_text(
        "Patent Number,Status,Title,Category\nUS 123,VERIFIED,Source title,Source category\n",
        encoding="utf-8",
    )
    importer = PatentImporter(tmp_path, no_network=True)
    importer.import_source(source)
    store = PatentStore(tmp_path)
    store.update_manual("US123", {"category": "Manual category"})
    before = store.get("US123")

    importer.import_source(source)
    after = PatentStore(tmp_path).get("US123")

    assert after == before
    assert after.category == "Manual category"


def test_verified_retrieval_saves_cropped_asset_upload_and_metadata(tmp_path: Path):
    source = tmp_path / "patents.csv"
    source.write_text(
        "Patent Number,Status,Source URL\n"
        "US123,VERIFIED,https://patents.google.com/patent/US123/en\n",
        encoding="utf-8",
    )
    upload_dir = tmp_path / "uploads"
    uploaded: list[Path] = []

    def retriever(_record):
        return RetrievalResult(
            html=(
                '<meta name="citation_pdf_url" '
                'content="https://patentimages.storage.googleapis.com/source.pdf">'
            ),
            pdf=b"%PDF-fake",
            drawing_images=[
                RetrievedDrawing(
                    "https://patentimages.storage.googleapis.com/small.png",
                    _png_bytes((20, 20), (5, 5, 14, 14)),
                ),
                RetrievedDrawing(
                    "https://patentimages.storage.googleapis.com/large.png",
                    _png_bytes((40, 30), (10, 7, 29, 22)),
                ),
            ],
        )

    result = PatentImporter(
        tmp_path,
        upload_dir=upload_dir,
        retriever=retriever,
        image_manager_callback=lambda path: uploaded.append(path),
    ).import_source(source)

    assert result.retrieved == 1
    asset_dir = tmp_path / "images" / "US123"
    assert (asset_dir / "source.pdf").read_bytes() == b"%PDF-fake"
    assert (asset_dir / "metadata.json").exists()
    assert (upload_dir / "patent-US123.png").exists()
    assert uploaded == [upload_dir / "patent-US123.png"]
    with Image.open(asset_dir / "original.png") as image:
        assert image.size == (36, 30)
    assert PatentStore(tmp_path).get("US123").verification_status == "asset_ready"


def test_retryable_retrieval_failure_is_recorded(tmp_path: Path):
    source = tmp_path / "patents.csv"
    source.write_text(
        "Patent Number,Status,Source URL\n"
        "US123,VERIFIED,https://patents.google.com/patent/US123/en\n",
        encoding="utf-8",
    )

    def failing_retriever(_record):
        raise TimeoutError("test timeout")

    result = PatentImporter(tmp_path, retriever=failing_retriever).import_source(source)
    errors = json.loads((tmp_path / "import-errors.json").read_text(encoding="utf-8"))

    assert result.errors[0]["patent"] == "US123"
    assert errors[0] == {
        "patent": "US123",
        "step": "retrieve",
        "error": "test timeout",
        "retryable": True,
    }
