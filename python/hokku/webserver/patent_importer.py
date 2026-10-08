"""Source ingestion and optional asset retrieval for Iconic Patents.

The source side is intentionally boring: CSV uses :class:`csv.DictReader`,
and XLSX uses only :mod:`zipfile` plus :mod:`xml.etree.ElementTree`.  Network
work is behind an injectable retriever so imports and tests can remain fully
offline.
"""

from __future__ import annotations

import csv
import html as html_lib
import inspect
import io
import json
import posixpath
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Protocol
from xml.etree import ElementTree

from PIL import Image, ImageChops

from hokku.webserver.filesystem import atomic_write_json
from hokku.webserver.patent_store import (
    PatentRecord,
    PatentStore,
    candidate_id,
    normalize_patent_number,
)
from hokku.webserver.patent_store import (
    normalize_status as _normalize_status,
)

_XLSX_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XLSX_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_ALLOWED_HOSTS = {"patents.google.com", "patentimages.storage.googleapis.com"}
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp")

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "simple_name": ("simple name", "name", "patent name"),
    "patent_title": ("patent title", "title", "invention title"),
    "inventor_names": (
        "inventor names",
        "inventor s",
        "inventors",
        "inventor",
        "inventor name",
    ),
    "patent_number": (
        "patent number",
        "patent no",
        "patent number id",
        "publication number",
        "publication no",
    ),
    "patent_year": ("patent year", "year"),
    "patent_date": ("patent date", "publication date", "date"),
    "description": ("description", "display description", "abstract", "summary"),
    "category": ("category", "categories", "classification"),
    "source_url": (
        "source url",
        "authoritative source url",
        "google patents url",
        "patent url",
        "url",
    ),
    "original_patent_document_url": (
        "original patent document url",
        "patent document url",
        "document url",
        "citation pdf url",
        "pdf url",
    ),
    "image_url": ("image url", "drawing url", "figure url"),
    "local_image_path": ("local image path",),
    "image_name": ("image name", "filename", "file name"),
    "recommended_figure": (
        "recommended figure",
        "recommended image",
        "recommended drawing",
    ),
    "recommended_page": ("recommended page", "page"),
    "qr_destination_url": ("qr destination url", "qr url"),
    "iconicity_score": ("iconicity score", "iconicity 1 5", "iconicity"),
    "visual_quality_score": (
        "visual quality score",
        "visual quality 1 5",
        "visual quality",
    ),
    "verification_status": ("verification status", "status", "verification"),
    "collection_ids": ("collection ids", "collections", "collection"),
    "manual_fields": ("manual fields",),
}


def normalize_status(value: object) -> str:
    """Public importer alias for the store's status normalizer."""

    return _normalize_status(value)


@dataclass(frozen=True)
class RetrievedDrawing:
    """One full-resolution drawing image returned by a retriever."""

    url: str
    content: bytes
    quality: float | None = None


@dataclass
class RetrievalResult:
    """Network-independent retrieval result consumed by :class:`PatentImporter`."""

    html: str | bytes = ""
    pdf: bytes | None = None
    drawing_images: list[RetrievedDrawing] = field(default_factory=list)
    citation_pdf_url: str | None = None


class PatentRetriever(Protocol):
    def retrieve(self, record: PatentRecord) -> RetrievalResult:
        """Fetch the authoritative HTML, source PDF, and drawing images."""


@dataclass(frozen=True)
class ParsedPatentPage:
    citation_pdf_url: str | None
    drawing_image_urls: list[str]


@dataclass
class ImportResult:
    records: list[PatentRecord]
    errors: list[dict[str, object]] = field(default_factory=list)
    retrieved: int = 0

    @property
    def imported(self) -> int:
        return len(self.records)


class PatentImportFailure(RuntimeError):
    """An importer failure with a manifest step and retryability flag."""

    def __init__(self, step: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.step = step
        self.retryable = retryable


def _clean_header(value: object) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).split())


def _clean_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_value(row: Mapping[str, object], aliases: tuple[str, ...]) -> str | None:
    normalized = {_clean_header(key): value for key, value in row.items()}
    for alias in aliases:
        value = _clean_value(normalized.get(_clean_header(alias)))
        if value is not None:
            return value
    return None


def _split_values(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, list):
        return [str(item).strip() for item in decoded if str(item).strip()]
    return [part.strip() for part in re.split(r"[;|\n]+", value) if part.strip()]


def _number(value: str | None) -> float | str | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return value


def _integer_if_possible(value: str | None) -> int | str | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def record_from_source_row(row_number: int, row: Mapping[str, object]) -> PatentRecord:
    """Map one source row without filling absent values from assumptions."""

    patent_number = normalize_patent_number(_first_value(row, _FIELD_ALIASES["patent_number"]))
    record_id = patent_number or candidate_id(row_number, row)
    patent_date = _first_value(row, _FIELD_ALIASES["patent_date"])
    source_url = _first_value(row, _FIELD_ALIASES["source_url"])
    patent_year = _integer_if_possible(_first_value(row, _FIELD_ALIASES["patent_year"]))
    if patent_year is None and patent_date:
        year_match = re.match(r"\s*(\d{4})", patent_date)
        if year_match:
            patent_year = int(year_match.group(1))
    return PatentRecord(
        id=record_id,
        simple_name=_first_value(row, _FIELD_ALIASES["simple_name"]),
        patent_title=_first_value(row, _FIELD_ALIASES["patent_title"]),
        inventor_names=_split_values(_first_value(row, _FIELD_ALIASES["inventor_names"])),
        patent_number=patent_number,
        patent_year=patent_year,
        patent_date=patent_date,
        description=_first_value(row, _FIELD_ALIASES["description"]),
        category=_first_value(row, _FIELD_ALIASES["category"]),
        source_url=source_url,
        original_patent_document_url=_first_value(
            row, _FIELD_ALIASES["original_patent_document_url"]
        ),
        image_url=_first_value(row, _FIELD_ALIASES["image_url"]),
        local_image_path=_first_value(row, _FIELD_ALIASES["local_image_path"]),
        image_name=_first_value(row, _FIELD_ALIASES["image_name"]),
        recommended_figure=_first_value(row, _FIELD_ALIASES["recommended_figure"]),
        recommended_page=_integer_if_possible(
            _first_value(row, _FIELD_ALIASES["recommended_page"])
        ),
        qr_destination_url=_first_value(row, _FIELD_ALIASES["qr_destination_url"]) or source_url,
        iconicity_score=_number(_first_value(row, _FIELD_ALIASES["iconicity_score"])),
        visual_quality_score=_number(_first_value(row, _FIELD_ALIASES["visual_quality_score"])),
        verification_status=normalize_status(
            _first_value(row, _FIELD_ALIASES["verification_status"])
        ),
        collection_ids=_split_values(_first_value(row, _FIELD_ALIASES["collection_ids"])),
        manual_fields=_split_values(_first_value(row, _FIELD_ALIASES["manual_fields"])),
        source_row={str(key): "" if value is None else str(value) for key, value in row.items()},
    )


def _xlsx_column_index(cell_reference: str) -> int:
    letters = re.match(r"[A-Za-z]+", cell_reference or "")
    if not letters:
        return 0
    index = 0
    for char in letters.group(0).upper():
        index = index * 26 + ord(char) - ord("A") + 1
    return index - 1


def _xml_text(element: ElementTree.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def read_xlsx_first_sheet(source: str | Path) -> list[tuple[int, dict[str, str]]]:
    """Read the first worksheet from an XLSX archive using stdlib XML only."""

    with zipfile.ZipFile(source) as archive:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))  # noqa: S314
        relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))  # noqa: S314
        relationship_targets = {
            relation.attrib["Id"]: relation.attrib["Target"]
            for relation in relationships.findall(f"{{{_PACKAGE_REL_NS}}}Relationship")
        }
        sheet = workbook.find(f"{{{_XLSX_MAIN_NS}}}sheets/{{{_XLSX_MAIN_NS}}}sheet")
        if sheet is None:
            return []
        relationship_id = sheet.attrib.get(f"{{{_XLSX_REL_NS}}}id") or sheet.attrib.get("id")
        target = relationship_targets.get(relationship_id or "")
        if not target:
            raise ValueError("first XLSX sheet has no relationship target")
        sheet_path = target.lstrip("/")
        if not sheet_path.startswith("xl/"):
            sheet_path = posixpath.normpath(posixpath.join("xl", sheet_path))
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))  # noqa: S314
            shared_strings = [
                _xml_text(item.find(f".//{{{_XLSX_MAIN_NS}}}t"))
                for item in shared_root.findall(f"{{{_XLSX_MAIN_NS}}}si")
            ]
        worksheet = ElementTree.fromstring(archive.read(sheet_path))  # noqa: S314
        raw_rows: list[tuple[int, dict[int, str]]] = []
        for fallback_number, row_element in enumerate(
            worksheet.findall(f".//{{{_XLSX_MAIN_NS}}}row"), start=1
        ):
            row_number = int(row_element.attrib.get("r", fallback_number))
            values: dict[int, str] = {}
            for cell in row_element.findall(f"{{{_XLSX_MAIN_NS}}}c"):
                column = _xlsx_column_index(cell.attrib.get("r", ""))
                value_type = cell.attrib.get("t")
                if value_type == "inlineStr":
                    value = _xml_text(cell.find(f"{{{_XLSX_MAIN_NS}}}is"))
                else:
                    value = _xml_text(cell.find(f"{{{_XLSX_MAIN_NS}}}v"))
                    if value_type == "s" and value:
                        value = shared_strings[int(value)]
                values[column] = value
            raw_rows.append((row_number, values))
    if not raw_rows:
        return []
    header_width = max((max(values, default=-1) for _, values in raw_rows), default=-1) + 1
    headers = [
        raw_rows[0][1].get(index, "") or f"column_{index + 1}" for index in range(header_width)
    ]
    return [
        (
            row_number,
            {headers[index]: values.get(index, "") for index in range(header_width)},
        )
        for row_number, values in raw_rows[1:]
    ]


def read_csv_rows(source: str | Path) -> list[tuple[int, dict[str, str]]]:
    """Read CSV rows with their one-based source row numbers."""

    with open(source, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows: list[tuple[int, dict[str, str]]] = []
        for row_number, row in enumerate(reader, start=2):
            values = {str(key): "" if value is None else value for key, value in row.items() if key}
            if None in row:
                for offset, value in enumerate(row[None] or [], start=len(values) + 1):
                    values[f"column_{offset}"] = value
            rows.append((row_number, values))
        return rows


def load_source_rows(source: str | Path) -> list[tuple[int, dict[str, str]]]:
    """Load all source rows, dispatching by the source filename suffix."""

    path = Path(source)
    if path.suffix.casefold() == ".xlsx":
        return read_xlsx_first_sheet(path)
    if path.suffix.casefold() == ".csv":
        return read_csv_rows(path)
    raise ValueError("source must be a .csv or .xlsx file")


def _allowed_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme == "https" and parsed.hostname in _ALLOWED_HOSTS


def is_authoritative_source_url(value: object) -> bool:
    """Whether a source URL is the allowed Google Patents authority."""

    return (
        isinstance(value, str)
        and urllib.parse.urlparse(value).hostname == "patents.google.com"
        and _allowed_url(value)
    )


class _PatentPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.citation_pdf_url: str | None = None
        self.drawing_image_urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): value for key, value in attrs}
        if tag.casefold() == "meta":
            name = (attributes.get("name") or attributes.get("property") or "").casefold()
            if name == "citation_pdf_url" and attributes.get("content"):
                self.citation_pdf_url = html_lib.unescape(attributes["content"] or "")
        for key in ("src", "data-src", "data-original", "href"):
            value = attributes.get(key)
            if value and _is_drawing_url(value):
                self.drawing_image_urls.append(html_lib.unescape(value))


def _is_drawing_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "patentimages.storage.googleapis.com"
        and parsed.path.casefold().endswith(_IMAGE_SUFFIXES)
    )


def parse_patent_html(document: str | bytes) -> ParsedPatentPage:
    """Extract the PDF citation and full drawing URLs from Google Patents HTML."""

    text = document.decode("utf-8", errors="replace") if isinstance(document, bytes) else document
    parser = _PatentPageParser()
    parser.feed(text)
    pdf_match = re.search(r"citation_pdf_url\s*[=:]\s*[\"']([^\"']+)", text, flags=re.IGNORECASE)
    citation_pdf_url = parser.citation_pdf_url or (
        html_lib.unescape(pdf_match.group(1)) if pdf_match else None
    )
    if citation_pdf_url and not _allowed_url(citation_pdf_url):
        citation_pdf_url = None
    urls = list(parser.drawing_image_urls)
    for match in re.findall(
        r"https://patentimages\.storage\.googleapis\.com/[^\"'<>\s]+",
        text,
        flags=re.IGNORECASE,
    ):
        clean = html_lib.unescape(match).rstrip(")],;")
        if _is_drawing_url(clean):
            urls.append(clean)
    return ParsedPatentPage(citation_pdf_url, list(dict.fromkeys(urls)))


class _RestrictedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        absolute = urllib.parse.urljoin(req.full_url, newurl)
        if not _allowed_url(absolute):
            raise PatentImportFailure(
                "retrieve", "redirected to an unapproved host", retryable=False
            )
        return super().redirect_request(req, fp, code, msg, headers, absolute)


def _fetch_url(url: str, *, timeout: float) -> bytes:
    if not _allowed_url(url):
        raise PatentImportFailure(
            "retrieve", f"unapproved retrieval host for {url}", retryable=False
        )
    request = urllib.request.Request(url, headers={"User-Agent": "hokku-iconic-patents/1.0"})
    opener = urllib.request.build_opener(_RestrictedRedirectHandler)
    try:
        with opener.open(request, timeout=timeout) as response:
            final_url = response.geturl()
            if not _allowed_url(final_url):
                raise PatentImportFailure(
                    "retrieve", "response came from an unapproved host", retryable=False
                )
            return response.read()
    except PatentImportFailure:
        raise
    except (OSError, urllib.error.URLError) as exc:
        raise PatentImportFailure("retrieve", str(exc), retryable=True) from exc


class HttpPatentRetriever:
    """Restricted Google Patents retriever used by the CLI when networking is enabled."""

    def __init__(self, *, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def retrieve(self, record: PatentRecord) -> RetrievalResult:
        if not record.patent_number or not is_authoritative_source_url(record.source_url):
            raise PatentImportFailure(
                "retrieve", "record lacks an authoritative source URL", retryable=False
            )
        page = _fetch_url(record.source_url, timeout=self.timeout)
        parsed = parse_patent_html(page)
        if not parsed.citation_pdf_url:
            raise PatentImportFailure(
                "parse_html", "citation_pdf_url was not present", retryable=True
            )
        if not parsed.drawing_image_urls:
            raise PatentImportFailure(
                "parse_html", "no full drawing image URLs were present", retryable=True
            )
        pdf = _fetch_url(parsed.citation_pdf_url, timeout=self.timeout)
        drawings = [
            RetrievedDrawing(url, _fetch_url(url, timeout=self.timeout))
            for url in parsed.drawing_image_urls
        ]
        return RetrievalResult(
            html=page,
            pdf=pdf,
            drawing_images=drawings,
            citation_pdf_url=parsed.citation_pdf_url,
        )


def _coerce_retrieval(value: object) -> RetrievalResult:
    if isinstance(value, RetrievalResult):
        return value
    if isinstance(value, Mapping):
        drawings = value.get("drawing_images", value.get("images", []))
        return RetrievalResult(
            html=value.get("html", ""),
            pdf=value.get("pdf", value.get("source_pdf")),
            drawing_images=_coerce_drawings(drawings),
            citation_pdf_url=value.get("citation_pdf_url"),
        )
    if isinstance(value, (tuple, list)):
        return RetrievalResult(
            html=value[0] if value else "",
            pdf=value[1] if len(value) > 1 else None,
            drawing_images=_coerce_drawings(value[2] if len(value) > 2 else []),
        )
    if isinstance(value, (str, bytes)):
        return RetrievalResult(html=value)
    raise TypeError("retriever must return RetrievalResult, a mapping, or a tuple")


def _coerce_drawings(value: object) -> list[RetrievedDrawing]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [RetrievedDrawing(str(url), bytes(content)) for url, content in value.items()]
    if not isinstance(value, (list, tuple)):
        return []
    drawings: list[RetrievedDrawing] = []
    for item in value:
        if isinstance(item, RetrievedDrawing):
            drawings.append(item)
        elif isinstance(item, Mapping):
            content = item.get("content", item.get("data", b""))
            drawings.append(
                RetrievedDrawing(
                    str(item.get("url", "")),
                    content if isinstance(content, bytes) else bytes(content),
                    float(item["quality"]) if item.get("quality") is not None else None,
                )
            )
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            drawings.append(RetrievedDrawing(str(item[0]), bytes(item[1])))
    return drawings


def _call_with_record(callback: object, record: PatentRecord) -> object:
    method = getattr(callback, "retrieve", callback)
    try:
        signature = inspect.signature(method)
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
    except (TypeError, ValueError):
        positional = [None]
    if len(positional) >= 2:
        return method(record.patent_number, record.source_url)
    return method(record)


def _call_image_manager(callback: Callable[..., object], path: Path, record: PatentRecord) -> None:
    try:
        signature = inspect.signature(callback)
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
    except (TypeError, ValueError):
        positional = [None]
    if len(positional) >= 2:
        callback(path, record)
    else:
        callback(path)


def crop_outer_white_margins(image: Image.Image, safe_margin: int = 8) -> Image.Image:
    """Crop only the outside white border, retaining a margin around the art.

    The threshold is used solely to find the crop rectangle.  The returned
    pixels are cropped from the original image without thresholding or other
    stylization.
    """

    if image.mode in {"RGBA", "LA"}:
        white = Image.new("RGBA", image.size, "white")
        white.alpha_composite(image.convert("RGBA"))
        rgb = white.convert("RGB")
    else:
        rgb = image.convert("RGB")
    masks = [channel.point(lambda pixel: 255 if pixel < 250 else 0, "L") for channel in rgb.split()]
    mask = ImageChops.lighter(ImageChops.lighter(masks[0], masks[1]), masks[2])
    bbox = mask.getbbox()
    if bbox is None:
        return image.copy()
    left, top, right, bottom = bbox
    margin = max(0, int(safe_margin))
    left = max(0, left - margin)
    top = max(0, top - margin)
    right = min(image.width, right + margin)
    bottom = min(image.height, bottom + margin)
    return image.crop((left, top, right, bottom))


class PatentImporter:
    """Import rows and, when authorized, retrieve verified patent assets."""

    def __init__(
        self,
        library_dir: str | Path,
        *,
        upload_dir: str | Path | None = None,
        store: PatentStore | None = None,
        retriever: PatentRetriever | Callable[..., object] | None = None,
        image_manager_callback: Callable[..., object] | None = None,
        no_network: bool = False,
        dry_run: bool = False,
    ) -> None:
        self.library_dir = Path(library_dir)
        self.upload_dir = Path(upload_dir) if upload_dir is not None else None
        self.store = store or PatentStore(self.library_dir)
        self.retriever = retriever or HttpPatentRetriever()
        self.image_manager_callback = image_manager_callback
        self.no_network = no_network
        self.dry_run = dry_run

    def import_source(self, source: str | Path, *, limit: int | None = None) -> ImportResult:
        source_path = Path(source).resolve()
        if source_path == self.store.csv_path.resolve() and self.store.list():
            records = self.store.list()
            if limit is not None:
                if limit < 0:
                    raise ValueError("limit must be non-negative")
                records = records[:limit]
            return ImportResult(records=records)
        rows = load_source_rows(source)
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be non-negative")
            rows = rows[:limit]
        records: list[PatentRecord] = []
        errors: list[dict[str, object]] = []
        retrieved = 0
        for row_number, row in rows:
            incoming = record_from_source_row(row_number, row)
            if self.dry_run:
                records.append(incoming)
                continue
            record = self.store.upsert(incoming)
            if self._should_retrieve(record):
                try:
                    result = _coerce_retrieval(_call_with_record(self.retriever, record))
                    record = self._save_retrieval(record, result)
                    retrieved += 1
                except Exception as exc:  # continue ingesting later source rows
                    error = self._error_entry(record, exc)
                    errors.append(error)
                    self._persist_error(error)
                    record = self.store.get(record.id) or record
            records.append(record)
        return ImportResult(records=records, errors=errors, retrieved=retrieved)

    def import_file(self, source: str | Path, *, limit: int | None = None) -> ImportResult:
        """Compatibility alias for callers that name the source a file."""

        return self.import_source(source, limit=limit)

    def _should_retrieve(self, record: PatentRecord) -> bool:
        if (
            self.no_network
            or not record.patent_number
            or not is_authoritative_source_url(record.source_url)
        ):
            return False
        if record.verification_status != "verified":
            return False
        if record.local_image_path:
            local_path = self.library_dir / record.local_image_path
            if local_path.exists():
                return False
        return True

    def _save_retrieval(self, record: PatentRecord, result: RetrievalResult) -> PatentRecord:
        parsed = parse_patent_html(result.html)
        citation_pdf_url = result.citation_pdf_url or parsed.citation_pdf_url
        if result.pdf is None:
            raise PatentImportFailure(
                "download_pdf", "retriever returned no source PDF", retryable=True
            )
        drawings = list(result.drawing_images)
        if not drawings:
            raise PatentImportFailure(
                "download_image", "retriever returned no drawing images", retryable=True
            )
        selected, image = _select_best_drawing(drawings)
        patent_number = normalize_patent_number(record.patent_number)
        if not patent_number:
            raise PatentImportFailure(
                "save_asset", "record has no canonical patent number", retryable=False
            )
        asset_dir = self.library_dir / "images" / patent_number
        asset_dir.mkdir(parents=True, exist_ok=True)
        original_path = asset_dir / "original.png"
        cropped = crop_outer_white_margins(image)
        cropped.save(original_path, format="PNG")
        (asset_dir / "source.pdf").write_bytes(result.pdf)
        relative_image_path = Path("images") / patent_number / "original.png"
        image_name = f"patent-{patent_number}.png"
        updated = self.store.mark_asset_ready(
            record.id,
            local_image_path=relative_image_path.as_posix(),
            image_name=image_name,
            image_url=selected.url or record.image_url,
            original_patent_document_url=citation_pdf_url or record.original_patent_document_url,
        )
        metadata = {
            "record": updated.to_dict(),
            "source_url": record.source_url,
            "citation_pdf_url": citation_pdf_url,
            "drawing_image_url": selected.url,
        }
        atomic_write_json(asset_dir / "metadata.json", metadata)
        if self.upload_dir is not None:
            self.upload_dir.mkdir(parents=True, exist_ok=True)
            upload_path = self.upload_dir / image_name
            shutil.copyfile(original_path, upload_path)
            if self.image_manager_callback is not None:
                try:
                    _call_image_manager(self.image_manager_callback, upload_path, updated)
                except Exception as exc:
                    raise PatentImportFailure("upload", str(exc), retryable=True) from exc
        return updated

    def _error_entry(self, record: PatentRecord, error: Exception) -> dict[str, object]:
        if isinstance(error, PatentImportFailure):
            step = error.step
            retryable = error.retryable
        else:
            step = "retrieve"
            retryable = not isinstance(error, (TypeError, ValueError, KeyError))
        return {
            "patent": record.patent_number or record.id,
            "step": step,
            "error": str(error),
            "retryable": retryable,
        }

    def _persist_error(self, error: dict[str, object]) -> None:
        self.library_dir.mkdir(parents=True, exist_ok=True)
        path = self.library_dir / "import-errors.json"
        previous: list[dict[str, object]] = []
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, list):
                    previous = [item for item in payload if isinstance(item, dict)]
                elif isinstance(payload, dict) and isinstance(payload.get("errors"), list):
                    previous = [item for item in payload["errors"] if isinstance(item, dict)]
            except (OSError, json.JSONDecodeError):
                previous = []
        atomic_write_json(path, [*previous, error])


def _select_best_drawing(drawings: list[RetrievedDrawing]) -> tuple[RetrievedDrawing, Image.Image]:
    candidates: list[tuple[tuple[float, int, int], RetrievedDrawing, Image.Image]] = []
    for drawing in drawings:
        try:
            with Image.open(io.BytesIO(drawing.content)) as opened:
                opened.load()
                image = opened.copy()
                area = image.width * image.height
                format_score = 1 if (opened.format or "").casefold() == "png" else 0
            quality = drawing.quality if drawing.quality is not None else 0.0
            candidates.append(((quality, area, format_score), drawing, image))
        except (OSError, ValueError):
            continue
    if not candidates:
        raise PatentImportFailure(
            "download_image", "drawing images were not decodable", retryable=True
        )
    _, drawing, image = max(candidates, key=lambda item: item[0])
    return drawing, image


__all__ = [
    "HttpPatentRetriever",
    "ImportResult",
    "ParsedPatentPage",
    "PatentImportFailure",
    "PatentImporter",
    "PatentRetriever",
    "RetrievalResult",
    "RetrievedDrawing",
    "crop_outer_white_margins",
    "is_authoritative_source_url",
    "load_source_rows",
    "normalize_status",
    "parse_patent_html",
    "read_csv_rows",
    "read_xlsx_first_sheet",
    "record_from_source_row",
]
