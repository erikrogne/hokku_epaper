"""Focused tests for the Iconic Patents command-line surface."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import ClassVar

import pytest

from hokku.webserver.app_config import AppConfig
from hokku.webserver.patent_store import PatentRecord, PatentStore

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import iconic_patents as cli  # noqa: E402


def _write_config(tmp_path: Path) -> Path:
    config = AppConfig().to_dict()
    config["upload_dir"] = str(tmp_path / "hokku-images")
    config["cache_dir"] = str(tmp_path / "hokku-cache")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _write_catalog(tmp_path: Path) -> Path:
    path = tmp_path / "catalog.csv"
    path.write_text(
        "Simple Name,Display Description,Category,Verification Status\n"
        "Test Airplane,An early airplane,transport,retrieve + verify\n",
        encoding="utf-8",
    )
    return path


def _write_bridge_catalog(tmp_path: Path) -> Path:
    path = tmp_path / "bridge-catalog.csv"
    path.write_text(
        "Simple Name,Patent Number,Verification Status,Image Name\n"
        "Test Airplane,US821393A,asset_ready,patent-US821393A.png\n",
        encoding="utf-8",
    )
    return path


def test_help_and_aliases_expose_config_library_and_png_export():
    parser = cli.build_parser()
    root_help = parser.format_help()
    assert "export-png" in root_help
    assert "offline PNGs" in root_help

    export_help = parser.parse_args(["export", "--output-dir", "out"])
    assert export_help.command == "export"
    assert export_help.orientation is None

    import_help = parser.parse_args(["import", "catalog.csv", "--config", "config.json"])
    assert import_help.config == Path("config.json")
    assert import_help.library_dir is None


def test_config_derives_library_upload_and_cache_paths(tmp_path: Path):
    config_path = _write_config(tmp_path)

    paths = cli.resolve_cli_paths(config_path=config_path)

    assert paths.library_dir == tmp_path / "iconic-patents"
    assert paths.upload_dir == tmp_path / "hokku-images"
    assert paths.cache_dir == tmp_path / "hokku-cache"

    override = cli.resolve_cli_paths(
        config_path=config_path,
        library_dir=tmp_path / "staging-library",
        upload_dir=tmp_path / "staging-images",
        cache_dir=tmp_path / "staging-cache",
    )
    assert override.library_dir == tmp_path / "staging-library"
    assert override.upload_dir == tmp_path / "staging-images"
    assert override.cache_dir == tmp_path / "staging-cache"


def test_import_uses_config_paths_without_network(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    config_path = _write_config(tmp_path)
    source = _write_catalog(tmp_path)

    result = cli.main(
        [
            "import",
            str(source),
            "--config",
            str(config_path),
            "--no-network",
            "--collection-id",
            "iconic-patents",
        ]
    )

    assert result == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["imported"] == 1
    assert summary["retrieved"] == 0
    assert summary["upload_dir"] == str(tmp_path / "hokku-images")
    assert summary["cache_dir"] == str(tmp_path / "hokku-cache")
    assert summary["automatic_collection"] == {
        "status": "skipped",
        "reason": "no bridge image was produced",
    }

    store = PatentStore(tmp_path / "iconic-patents")
    records = store.list()
    assert len(records) == 1
    assert records[0].collection_ids == ["iconic-patents"]
    assert not (tmp_path / "hokku-images").exists()
    assert not (tmp_path / "hokku-cache" / "collections.json").exists()


def test_config_import_creates_iconic_collection_and_adds_bridge_membership(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    config_path = _write_config(tmp_path)
    source = _write_bridge_catalog(tmp_path)
    upload_dir = tmp_path / "hokku-images"
    upload_dir.mkdir()
    (upload_dir / "patent-US821393A.png").write_bytes(b"bridge")

    result = cli.main(
        [
            "import",
            str(source),
            "--config",
            str(config_path),
            "--no-network",
            "--collection-id",
            "manual-collection",
        ]
    )

    assert result == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["automatic_collection"]["status"] == "updated"
    assert summary["automatic_collection"]["name"] == "Iconic Patents"
    assert summary["automatic_collection"]["bridge_images"] == ["patent-US821393A.png"]

    collection_payload = json.loads(
        (tmp_path / "hokku-cache" / "collections.json").read_text(encoding="utf-8")
    )
    iconic = next(
        collection
        for collection in collection_payload["collections"]
        if collection["name"] == "Iconic Patents"
    )
    assert collection_payload["memberships"][iconic["id"]] == ["patent-US821393A.png"]

    store = PatentStore(tmp_path / "iconic-patents")
    assert store.list()[0].collection_ids == ["manual-collection"]


def test_config_import_without_bridge_file_skips_collection_state(tmp_path: Path, capsys):
    config_path = _write_config(tmp_path)
    source = _write_bridge_catalog(tmp_path)

    result = cli.main(
        [
            "import",
            str(source),
            "--config",
            str(config_path),
            "--no-network",
        ]
    )

    assert result == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["automatic_collection"] == {
        "status": "skipped",
        "reason": "no bridge image was produced",
    }
    assert not (tmp_path / "hokku-cache" / "collections.json").exists()


def test_configless_import_does_not_create_hokku_collection_state(tmp_path: Path):
    source = _write_bridge_catalog(tmp_path)
    library_dir = tmp_path / "library"
    upload_dir = tmp_path / "images"

    result = cli.main(
        [
            "import",
            str(source),
            "--library-dir",
            str(library_dir),
            "--upload-dir",
            str(upload_dir),
            "--no-network",
        ]
    )

    assert result == 0
    assert not (library_dir / "collections.json").exists()


class _FakeRenderer:
    instances: ClassVar[list[_FakeRenderer]] = []

    def __init__(self, library_dir, image_config, *, cache_dir):
        self.library_dir = Path(library_dir)
        self.image_config = image_config
        self.cache_dir = Path(cache_dir)
        self.calls: list[tuple[str, str, Path]] = []
        self.instances.append(self)

    def export_png(self, record, model, orientation, destination):
        destination = Path(destination)
        destination.write_bytes(b"fake-png")
        self.calls.append((model, orientation, destination))
        return destination


def _export_store(tmp_path: Path) -> PatentStore:
    store = PatentStore(tmp_path / "library")
    store.upsert(
        PatentRecord(
            id="patent-1",
            simple_name="Airplane / Test",
            patent_number="US821393A",
            verification_status="display_ready",
            source_url="https://patents.google.com/patent/US821393A/en",
            local_image_path="images/US821393A/original.png",
        )
    )
    store.upsert(
        PatentRecord(
            id="candidate-2",
            simple_name="Not ready",
            verification_status="asset_ready",
            local_image_path="images/candidate-2/original.png",
        )
    )
    return store


def test_export_png_injects_renderer_filters_records_and_writes_manifest(tmp_path: Path):
    _FakeRenderer.instances.clear()
    store = _export_store(tmp_path)

    manifest_path, entries, errors = cli.export_png_records(
        store,
        output_dir=tmp_path / "exports",
        model="test-display",
        orientation="portrait",
        image_config=AppConfig().image_config_default,
        cache_dir=tmp_path / "cache",
        renderer_factory=_FakeRenderer,
    )

    assert errors == []
    assert len(entries) == 1
    output_path = tmp_path / "exports" / "0001-Airplane-Test-US821393A.png"
    assert output_path.read_bytes() == b"fake-png"
    assert manifest_path == tmp_path / "exports" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["model"] == "test-display"
    assert manifest["orientation"] == "portrait"
    assert manifest["records"][0]["output_path"] == output_path.name
    assert manifest["records"][0]["verification_status"] == "display_ready"

    renderer = _FakeRenderer.instances[0]
    assert renderer.library_dir == tmp_path / "library"
    assert renderer.cache_dir == tmp_path / "cache"
    assert renderer.calls == [("test-display", "portrait", output_path)]


def test_export_command_accepts_config_and_reports_render_errors(tmp_path: Path, capsys):
    config_path = _write_config(tmp_path)
    store = _export_store(tmp_path)
    store.save()
    args = cli.build_parser().parse_args(
        [
            "export-png",
            "--config",
            str(config_path),
            "--library-dir",
            str(store.library_dir),
            "--output-dir",
            str(tmp_path / "exports"),
        ]
    )

    class _FailingRenderer(_FakeRenderer):
        def export_png(self, record, model, orientation, destination):
            raise RuntimeError("renderer unavailable")

    result = cli._run_export_png(args, renderer_factory=_FailingRenderer)

    assert result == 2
    summary = json.loads(capsys.readouterr().out)
    assert summary["exported"] == 0
    assert summary["errors"] == [{"id": "patent-1", "error": "renderer unavailable"}]
    assert summary["model"] == "huessen_epf1301"
    assert summary["orientation"] == "portrait"
    manifest = json.loads((tmp_path / "exports" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["errors"] == summary["errors"]


def test_missing_config_is_a_concise_cli_error(tmp_path: Path, capsys):
    result = cli.main(
        [
            "export-manifest",
            "--config",
            str(tmp_path / "missing.json"),
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "error: config file not found" in captured.err
    assert "Traceback" not in captured.err
