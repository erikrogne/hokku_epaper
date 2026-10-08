"""Persistent records for the Iconic Patents library.

The patent library deliberately has its own manifest rather than extending the
image manager's database.  Source imports are additive and merge into an
existing record, while explicit manual edits are protected by ``manual_fields``.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from hokku.webserver.filesystem import atomic_write_json

PATENT_STATUSES = (
    "candidate",
    "needs_verification",
    "verified",
    "asset_ready",
    "display_ready",
    "rejected",
)

_STATUS_ALIASES = {
    "": "candidate",
    "candidate": "candidate",
    "new": "candidate",
    "unverified": "candidate",
    "needs verification": "needs_verification",
    "needs_verification": "needs_verification",
    "retrieve": "needs_verification",
    "verify": "needs_verification",
    "retrieve + verify": "needs_verification",
    "retrieve+verify": "needs_verification",
    "retrieve and verify": "needs_verification",
    "verified": "verified",
    "asset ready": "asset_ready",
    "asset_ready": "asset_ready",
    "display ready": "display_ready",
    "display_ready": "display_ready",
    "rejected": "rejected",
}

_MANIFEST_VERSION = 1
_CSV_FIELDS = (
    "id",
    "type",
    "simple_name",
    "patent_title",
    "inventor_names",
    "patent_number",
    "patent_year",
    "patent_date",
    "description",
    "category",
    "source_url",
    "original_patent_document_url",
    "image_url",
    "local_image_path",
    "image_name",
    "recommended_figure",
    "recommended_page",
    "qr_destination_url",
    "iconicity_score",
    "visual_quality_score",
    "verification_status",
    "collection_ids",
    "manual_fields",
    "source_row",
    "updated_at",
)

_MERGE_FIELDS = tuple(
    name for name in _CSV_FIELDS if name not in {"id", "type", "manual_fields", "updated_at"}
)


def normalize_status(value: object) -> str:
    """Return one of the finite statuses used by the patent manifest."""

    if value is None:
        return "candidate"
    clean = " ".join(str(value).strip().casefold().replace("_", " ").split())
    normalized = _STATUS_ALIASES.get(clean)
    if normalized is not None:
        return normalized
    return clean.replace(" ", "_") if clean.replace(" ", "_") in PATENT_STATUSES else "candidate"


def normalize_patent_number(value: object) -> str | None:
    """Normalize a source patent number into a stable, path-safe key.

    Punctuation and whitespace are presentation details for patent identifiers.
    The original spelling remains available in ``source_row``.
    """

    if value is None:
        return None
    clean = "".join(char for char in str(value).strip().upper() if char.isalnum())
    return clean or None


def candidate_id(row_number: int, row: Mapping[str, object]) -> str:
    """Build the deterministic ID for a row without a patent number."""

    payload = json.dumps(
        {"row_number": int(row_number), "row": dict(row)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"candidate-{hashlib.sha1(payload, usedforsecurity=False).hexdigest()}"


def _is_empty(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return not value
    return False


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, list):
            return _string_list(decoded)
        return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
    return [str(value).strip()]


def _source_row(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): "" if item is None else str(item) for key, item in value.items()}


@dataclass
class PatentRecord:
    """A serializable, source-backed patent record.

    ``None`` and empty collections mean that the source did not provide a
    value.  The importer never invents a replacement value for those fields.
    """

    id: str
    type: str = "patent"
    simple_name: str | None = None
    patent_title: str | None = None
    inventor_names: list[str] = field(default_factory=list)
    patent_number: str | None = None
    patent_year: int | str | None = None
    patent_date: str | None = None
    description: str | None = None
    category: str | None = None
    source_url: str | None = None
    original_patent_document_url: str | None = None
    image_url: str | None = None
    local_image_path: str | None = None
    image_name: str | None = None
    recommended_figure: str | None = None
    recommended_page: int | str | None = None
    qr_destination_url: str | None = None
    iconicity_score: float | str | None = None
    visual_quality_score: float | str | None = None
    verification_status: str = "candidate"
    collection_ids: list[str] = field(default_factory=list)
    manual_fields: list[str] = field(default_factory=list)
    source_row: dict[str, str] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.id = str(self.id)
        self.type = "patent"
        self.verification_status = normalize_status(self.verification_status)
        self.inventor_names = _string_list(self.inventor_names)
        self.patent_number = normalize_patent_number(self.patent_number)
        self.collection_ids = sorted(set(_string_list(self.collection_ids)))
        self.manual_fields = sorted(set(_string_list(self.manual_fields)))
        self.source_row = _source_row(self.source_row)
        try:
            self.updated_at = float(self.updated_at)
        except (TypeError, ValueError):
            self.updated_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        """Return only JSON-compatible values in the stable field order."""

        return {
            "id": self.id,
            "type": "patent",
            "simple_name": self.simple_name,
            "patent_title": self.patent_title,
            "inventor_names": list(self.inventor_names),
            "patent_number": self.patent_number,
            "patent_year": self.patent_year,
            "patent_date": self.patent_date,
            "description": self.description,
            "category": self.category,
            "source_url": self.source_url,
            "original_patent_document_url": self.original_patent_document_url,
            "image_url": self.image_url,
            "local_image_path": self.local_image_path,
            "image_name": self.image_name,
            "recommended_figure": self.recommended_figure,
            "recommended_page": self.recommended_page,
            "qr_destination_url": self.qr_destination_url,
            "iconicity_score": self.iconicity_score,
            "visual_quality_score": self.visual_quality_score,
            "verification_status": self.verification_status,
            "collection_ids": list(self.collection_ids),
            "manual_fields": list(self.manual_fields),
            "source_row": dict(self.source_row),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PatentRecord:
        """Load a record while tolerating older or hand-edited manifests."""

        if not data.get("id"):
            raise ValueError("patent record is missing id")
        values = {name: data.get(name) for name in _CSV_FIELDS if name in data}
        values.setdefault("id", data["id"])
        return cls(**values)


class PatentStore:
    """Thread-safe atomic manifest and CSV persistence for patent records."""

    def __init__(self, library_dir: str | Path) -> None:
        self.library_dir = Path(library_dir)
        self.manifest_path = self.library_dir / "manifest.json"
        self.csv_path = self.library_dir / "patents.csv"
        self._lock = threading.RLock()
        self._records: dict[str, PatentRecord] = {}
        self._load()

    def list(self) -> list[PatentRecord]:
        with self._lock:
            return [self._records[key] for key in sorted(self._records)]

    def get(self, record_id: str) -> PatentRecord | None:
        with self._lock:
            return self._records.get(str(record_id))

    def by_image_name(self, image_name: str) -> PatentRecord | None:
        if not image_name:
            return None
        with self._lock:
            for record in self.list():
                if record.image_name == image_name:
                    return record
        return None

    def upsert(self, record: PatentRecord | Mapping[str, object]) -> PatentRecord:
        """Merge a source record, preserving every existing manual field.

        A non-empty incoming source value updates a non-manual field.  Empty
        source cells never erase existing data.  Patent numbers are also used
        as a defensive deduplication key if a caller supplies a mismatched ID.
        """

        incoming = record if isinstance(record, PatentRecord) else PatentRecord.from_dict(record)
        with self._lock:
            existing_id = incoming.id
            if incoming.patent_number:
                normalized_number = normalize_patent_number(incoming.patent_number)
                for candidate in self._records.values():
                    if normalize_patent_number(candidate.patent_number) == normalized_number:
                        existing_id = candidate.id
                        break
            existing = self._records.get(existing_id)
            if existing is None:
                saved = replace(
                    incoming, patent_number=normalize_patent_number(incoming.patent_number)
                )
                self._records[saved.id] = saved
                self._save_locked()
                return saved

            manual_fields = sorted(set(existing.manual_fields) | set(incoming.manual_fields))
            values: dict[str, Any] = {}
            for name in _MERGE_FIELDS:
                current = getattr(existing, name)
                proposed = getattr(incoming, name)
                if name in existing.manual_fields:
                    values[name] = current
                elif name == "verification_status":
                    values[name] = _merge_status(current, proposed)
                elif _is_empty(proposed):
                    values[name] = current
                else:
                    values[name] = proposed
            merged = PatentRecord(
                id=existing.id,
                manual_fields=manual_fields,
                updated_at=existing.updated_at,
                **values,
            )
            if merged.to_dict() != existing.to_dict():
                merged.updated_at = time.time()
                self._records[existing.id] = merged
                self._save_locked()
            return self._records[existing.id]

    def update_manual(
        self,
        record_id: str,
        updates: Mapping[str, object] | str | None = None,
        value: object = None,
        **kwargs: object,
    ) -> PatentRecord:
        """Set caller-authored fields and mark them protected on future imports."""

        if isinstance(updates, str):
            changes: dict[str, object] = {updates: value}
        elif updates is None:
            changes = {}
        else:
            changes = dict(updates)
        changes.update(kwargs)
        with self._lock:
            current = self._require(record_id)
            self._validate_update_fields(changes)
            manual_fields = sorted(set(current.manual_fields) | set(changes))
            updated = replace(
                current, manual_fields=manual_fields, **changes, updated_at=time.time()
            )
            self._records[current.id] = updated
            self._save_locked()
            return updated

    def mark_asset_ready(
        self,
        record_id: str,
        local_image_path: str | None = None,
        image_name: str | None = None,
        **updates: object,
    ) -> PatentRecord:
        """Attach local asset metadata and advance a record to ``asset_ready``."""

        if local_image_path is not None:
            updates["local_image_path"] = local_image_path
        if image_name is not None:
            updates["image_name"] = image_name
        updates["verification_status"] = "asset_ready"
        with self._lock:
            current = self._require(record_id)
            self._validate_update_fields(updates, allow_status=True)
            values = {
                name: value
                for name, value in updates.items()
                if name not in current.manual_fields or name == "verification_status"
            }
            if current.verification_status == "display_ready":
                values["verification_status"] = current.verification_status
            updated = replace(current, **values, updated_at=time.time())
            self._records[current.id] = updated
            self._save_locked()
            return updated

    def set_collection_ids(
        self, record_id: str, collection_ids: list[str] | set[str]
    ) -> PatentRecord:
        """Replace collection membership without changing other source fields."""

        if not isinstance(collection_ids, (list, set, tuple)):
            raise ValueError("collection_ids must be a list or set of strings")
        ids = sorted({str(value).strip() for value in collection_ids if str(value).strip()})
        with self._lock:
            current = self._require(record_id)
            updated = replace(current, collection_ids=ids, updated_at=time.time())
            self._records[current.id] = updated
            self._save_locked()
            return updated

    def save(self) -> None:
        """Rewrite both derived files from the current in-memory records."""

        with self._lock:
            self._save_locked()

    def _require(self, record_id: str) -> PatentRecord:
        try:
            return self._records[str(record_id)]
        except KeyError as exc:
            raise KeyError(f"unknown patent record: {record_id}") from exc

    @staticmethod
    def _validate_update_fields(
        updates: Mapping[str, object], *, allow_status: bool = False
    ) -> None:
        allowed = set(_MERGE_FIELDS)
        if allow_status:
            allowed.add("verification_status")
        invalid = set(updates) - allowed
        if invalid:
            raise ValueError(f"unsupported patent fields: {', '.join(sorted(invalid))}")

    def _load(self) -> None:
        if not self.manifest_path.exists():
            return
        try:
            with self.manifest_path.open(encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return
        raw_records = payload.get("records", []) if isinstance(payload, dict) else payload
        if isinstance(raw_records, dict):
            raw_records = list(raw_records.values())
        if not isinstance(raw_records, list):
            return
        for raw_record in raw_records:
            if not isinstance(raw_record, Mapping):
                continue
            try:
                record = PatentRecord.from_dict(raw_record)
            except (TypeError, ValueError):
                continue
            self._records[record.id] = record

    def _save_locked(self) -> None:
        self.library_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            self.manifest_path,
            {
                "version": _MANIFEST_VERSION,
                "records": [record.to_dict() for record in self.list()],
            },
        )
        temporary = self.csv_path.with_suffix(".csv.tmp")
        try:
            with temporary.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=_CSV_FIELDS)
                writer.writeheader()
                for record in self.list():
                    writer.writerow(
                        {name: _csv_value(getattr(record, name)) for name in _CSV_FIELDS}
                    )
            os.replace(temporary, self.csv_path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


def _csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _merge_status(current: object, proposed: object) -> str:
    current_status = normalize_status(current)
    proposed_status = normalize_status(proposed)
    if current_status in {"asset_ready", "display_ready"} and proposed_status not in {
        "asset_ready",
        "display_ready",
    }:
        return current_status
    if current_status == "rejected" and proposed_status != "rejected":
        return proposed_status
    return proposed_status


__all__ = [
    "PATENT_STATUSES",
    "PatentRecord",
    "PatentStore",
    "candidate_id",
    "normalize_patent_number",
    "normalize_status",
]
