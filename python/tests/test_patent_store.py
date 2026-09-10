"""Focused persistence and merge tests for the iconic-patents store."""

from __future__ import annotations

from pathlib import Path

from hokku.webserver.patent_store import PatentRecord, PatentStore


def _record(**overrides) -> PatentRecord:
    values = {
        "id": "US1234567B2",
        "simple_name": "Original name",
        "patent_title": "Original title",
        "patent_number": "US1234567B2",
        "verification_status": "verified",
        "source_row": {"Patent Number": "US 1234567 B2"},
    }
    values.update(overrides)
    return PatentRecord(**values)


def test_record_round_trip_and_atomic_outputs(tmp_path: Path):
    store = PatentStore(tmp_path)
    saved = store.upsert(_record(collection_ids=["featured"], inventor_names=["Ada Lovelace"]))

    restored = PatentStore(tmp_path).get(saved.id)

    assert restored is not None
    assert restored == saved
    assert (tmp_path / "manifest.json").exists()
    assert (tmp_path / "patents.csv").exists()


def test_upsert_deduplicates_by_patent_number_and_preserves_manual_fields(tmp_path: Path):
    store = PatentStore(tmp_path)
    store.upsert(_record(category="Source category", image_name="source.png"))
    store.update_manual(_record().id, {"category": "Hand picked", "image_name": "manual.png"})

    merged = store.upsert(
        _record(
            simple_name="Refreshed name",
            patent_title="Refreshed title",
            category="New source category",
            image_name="new-source.png",
            source_row={"Patent Number": "US 1234567 B2", "Title": "Refreshed title"},
        )
    )

    assert len(store.list()) == 1
    assert merged.id == "US1234567B2"
    assert merged.simple_name == "Refreshed name"
    assert merged.patent_title == "Refreshed title"
    assert merged.category == "Hand picked"
    assert merged.image_name == "manual.png"
    assert set(merged.manual_fields) == {"category", "image_name"}


def test_asset_and_collection_helpers_update_one_record(tmp_path: Path):
    store = PatentStore(tmp_path)
    store.upsert(_record())

    ready = store.mark_asset_ready(
        "US1234567B2",
        local_image_path="images/US1234567B2/original.png",
        image_name="patent-US1234567B2.png",
        image_url="https://patentimages.storage.googleapis.com/drawing.png",
    )
    store.set_collection_ids(ready.id, ["featured", "archive", "featured"])

    assert store.by_image_name("patent-US1234567B2.png").id == ready.id
    assert store.get(ready.id).verification_status == "asset_ready"
    assert store.get(ready.id).collection_ids == ["archive", "featured"]
