#!/usr/bin/env python3
"""Import and export the Iconic Patents library.

The CLI can use a Hokku ``config.json`` as the source of truth for the normal
image upload directory, render cache, and default patent-library location.
Explicit directory options remain available for offline work and tests.

Examples::

    python tools/iconic_patents.py import candidates.csv \
        --config /var/lib/hokku/config.json --no-network
    python tools/iconic_patents.py export-png \
        --config /var/lib/hokku/config.json --output-dir exports/iconic-patents

``--dry-run`` parses and reports source rows without writing the manifest,
assets, or retry-error file.  It is intended for checking a source before a
real import.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYTHON_ROOT = _REPO_ROOT / "python"
if str(_PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(_PYTHON_ROOT))

from hokku.webserver.app_config import AppConfig  # noqa: E402
from hokku.webserver.collection_store import CollectionStore  # noqa: E402
from hokku.webserver.patent_importer import PatentImporter  # noqa: E402
from hokku.webserver.patent_renderer import (  # noqa: E402
    PatentRenderer,
    default_patent_orientation,
)
from hokku.webserver.patent_store import PatentRecord, PatentStore  # noqa: E402

_DEFAULT_LIBRARY_DIR = Path("data") / "iconic-patents"
_DEFAULT_EXPORT_DIR = Path("exports") / "iconic-patents"
_ICONIC_PATENTS_COLLECTION_NAME = "Iconic Patents"


@dataclass(frozen=True)
class CliPaths:
    """Resolved filesystem paths and the image settings used by the CLI."""

    config: AppConfig
    config_path: Path | None
    library_dir: Path
    upload_dir: Path | None
    cache_dir: Path


class CliError(ValueError):
    """An expected command-line configuration or input error."""


def _load_config(path: Path) -> AppConfig:
    """Load a config without AppConfig.load's missing-file self-healing.

    A command-line dry run should not create a config file as a side effect,
    and a typo in ``--config`` should be reported as a concise CLI error.
    ``AppConfig.from_dict`` still supplies the normal migration/default logic.
    """

    if not path.is_file():
        raise CliError(f"config file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return AppConfig.from_dict(payload)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CliError(f"could not load config {path}: {exc}") from exc


def resolve_cli_paths(
    *,
    config_path: str | Path | None = None,
    library_dir: str | Path | None = None,
    upload_dir: str | Path | None = None,
    cache_dir: str | Path | None = None,
) -> CliPaths:
    """Resolve CLI directories with explicit options taking precedence.

    With ``--config``, Hokku's configured cache parent is used for the patent
    library, e.g. ``/var/lib/hokku/cache`` becomes
    ``/var/lib/hokku/iconic-patents``.  Without a config, the CLI stays safe
    and local by defaulting to ``data/iconic-patents`` and does not assume that
    the system's default upload directory is writable.
    """

    normalized_config_path = Path(config_path).expanduser() if config_path else None
    config = _load_config(normalized_config_path) if normalized_config_path else AppConfig()

    configured_cache = Path(config.cache_dir).expanduser()
    configured_upload = Path(config.upload_dir).expanduser()
    resolved_library = (
        Path(library_dir).expanduser()
        if library_dir is not None
        else (
            configured_cache.parent / "iconic-patents"
            if normalized_config_path
            else _DEFAULT_LIBRARY_DIR
        )
    )
    resolved_cache = (
        Path(cache_dir).expanduser()
        if cache_dir is not None
        else (configured_cache if normalized_config_path else resolved_library)
    )
    resolved_upload = (
        Path(upload_dir).expanduser()
        if upload_dir is not None
        else (configured_upload if normalized_config_path else None)
    )
    return CliPaths(
        config=config,
        config_path=normalized_config_path,
        library_dir=resolved_library,
        upload_dir=resolved_upload,
        cache_dir=resolved_cache,
    )


def _add_path_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        help=(
            "Hokku config.json; derives upload/cache paths and the default "
            "patent library directory."
        ),
    )
    parser.add_argument(
        "--library-dir",
        type=Path,
        help=(
            "Override the patent library directory containing manifest.json, "
            "patents.csv, images/, and import-errors.json."
        ),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        help="Override the render cache directory; otherwise use AppConfig.cache_dir.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import and export the Iconic Patents library without changing Hokku firmware."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    import_parser = commands.add_parser(
        "import", help="Import CSV/XLSX source rows and optional verified assets."
    )
    import_parser.add_argument("source", type=Path, help="CSV or XLSX source file.")
    _add_path_args(import_parser)
    import_parser.add_argument(
        "--upload-dir",
        type=Path,
        help="Override the Hokku upload directory for patent-<PATENT>.png bridge copies.",
    )
    import_parser.add_argument("--limit", type=int, help="Import at most this many source rows.")
    import_parser.add_argument(
        "--no-network",
        action="store_true",
        help="Persist source rows but never attempt verified-patent retrieval.",
    )
    import_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report rows only; do not write candidates, assets, or import-errors.json.",
    )
    import_parser.add_argument(
        "--collection-id",
        action="append",
        default=[],
        help=(
            "Add this existing collection ID to imported records (repeatable; "
            "no collection is created)."
        ),
    )

    manifest_parser = commands.add_parser(
        "export-manifest",
        help="Rewrite patents.csv and manifest.json from the current store.",
    )
    _add_path_args(manifest_parser)

    export_parser = commands.add_parser(
        "export-png",
        aliases=["export"],
        help="Render display-ready records to offline PNGs and write an export manifest.",
    )
    _add_path_args(export_parser)
    export_parser.add_argument(
        "--output-dir",
        type=Path,
        default=_DEFAULT_EXPORT_DIR,
        help="Directory for PNGs and manifest.json (default: exports/iconic-patents).",
    )
    export_parser.add_argument(
        "--model",
        default="huessen_epf1301",
        help="Hokku display model ID (default: huessen_epf1301).",
    )
    export_parser.add_argument(
        "--orientation",
        choices=("landscape", "portrait"),
        default=None,
        help=(
            "Visible output orientation (default: portrait for huessen_epf1301, "
            "landscape for other models)."
        ),
    )
    export_parser.add_argument(
        "--limit", type=int, help="Export at most this many display-ready records."
    )
    return parser


def _resolve_args_paths(args: argparse.Namespace) -> CliPaths:
    return resolve_cli_paths(
        config_path=args.config,
        library_dir=args.library_dir,
        upload_dir=getattr(args, "upload_dir", None),
        cache_dir=args.cache_dir,
    )


def _ensure_iconic_patents_collection(
    paths: CliPaths,
    records: list[PatentRecord],
    *,
    dry_run: bool,
) -> dict[str, object]:
    """Add imported bridge images to the real Hokku collection when safe.

    A config-backed run is the authority for Hokku state paths.  Candidate-only
    imports, dry runs, and config-less local imports must not create an empty
    ``collections.json`` as a surprising side effect.
    """

    if dry_run:
        return {"status": "skipped", "reason": "dry-run"}
    if paths.config_path is None:
        return {"status": "skipped", "reason": "--config is required"}
    if paths.upload_dir is None:
        return {"status": "skipped", "reason": "no upload directory is configured"}

    bridge_names = sorted(
        {
            record.image_name
            for record in records
            if record.image_name
            and record.image_name.strip()
            and (paths.upload_dir / record.image_name).is_file()
        }
    )
    if not bridge_names:
        return {"status": "skipped", "reason": "no bridge image was produced"}

    collections = CollectionStore(paths.cache_dir)
    existing = collections.find_by_name(_ICONIC_PATENTS_COLLECTION_NAME)
    collection = collections.ensure(_ICONIC_PATENTS_COLLECTION_NAME)
    added = collections.add_images(collection.id, bridge_names)
    return {
        "status": "updated",
        "name": collection.name,
        "collection_id": collection.id,
        "created": existing is None,
        "bridge_images": bridge_names,
        "added": added,
    }


def _run_import(args: argparse.Namespace) -> int:
    paths = _resolve_args_paths(args)
    importer = PatentImporter(
        paths.library_dir,
        upload_dir=paths.upload_dir,
        no_network=args.no_network,
        dry_run=args.dry_run,
    )
    result = importer.import_source(args.source, limit=args.limit)
    if args.collection_id and not args.dry_run:
        for record in result.records:
            current = importer.store.get(record.id)
            if current is None:
                continue
            memberships = sorted(set(current.collection_ids) | set(args.collection_id))
            importer.store.set_collection_ids(current.id, memberships)
    automatic_collection = _ensure_iconic_patents_collection(
        paths,
        result.records,
        dry_run=args.dry_run,
    )
    print(
        json.dumps(
            {
                "imported": result.imported,
                "retrieved": result.retrieved,
                "errors": result.errors,
                "dry_run": args.dry_run,
                "config": str(paths.config_path) if paths.config_path else None,
                "library_dir": str(paths.library_dir),
                "upload_dir": str(paths.upload_dir) if paths.upload_dir else None,
                "cache_dir": str(paths.cache_dir),
                "automatic_collection": automatic_collection,
            },
            indent=2,
        )
    )
    return 0 if not result.errors else 2


def _run_export_manifest(args: argparse.Namespace) -> int:
    paths = _resolve_args_paths(args)
    store = PatentStore(paths.library_dir)
    store.save()
    print(
        json.dumps(
            {
                "exported": len(store.list()),
                "manifest": str(store.manifest_path),
                "csv": str(store.csv_path),
                "config": str(paths.config_path) if paths.config_path else None,
                "library_dir": str(paths.library_dir),
            },
            indent=2,
        )
    )
    return 0


def _safe_filename_part(value: object, fallback: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-._")
    return normalized or fallback


def _export_filename(index: int, record: PatentRecord) -> str:
    name = _safe_filename_part(record.simple_name, "patent")
    identifier = _safe_filename_part(record.patent_number or record.id, "record")
    return f"{index:04d}-{name}-{identifier}.png"


def _display_ready_records(store: PatentStore, limit: int | None) -> list[PatentRecord]:
    if limit is not None and limit < 0:
        raise CliError("limit must be non-negative")
    records = [record for record in store.list() if record.verification_status == "display_ready"]
    return records if limit is None else records[:limit]


def export_png_records(
    store: PatentStore,
    *,
    output_dir: str | Path,
    model: str,
    orientation: str,
    image_config,
    cache_dir: str | Path | None = None,
    renderer_factory: Callable[..., PatentRenderer] = PatentRenderer,
    limit: int | None = None,
) -> tuple[Path, list[dict[str, object]], list[dict[str, str]]]:
    """Render display-ready records and write a deterministic export manifest.

    ``renderer_factory`` is deliberately injectable so this command can be
    tested without running the real dither pipeline or touching network data.
    The production default is :class:`PatentRenderer`.
    """

    records = _display_ready_records(store, limit)
    destination_dir = Path(output_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    renderer = renderer_factory(
        store.library_dir,
        image_config,
        cache_dir=cache_dir,
    )
    entries: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    for index, record in enumerate(records, start=1):
        relative_path = Path(_export_filename(index, record))
        destination = destination_dir / relative_path
        try:
            renderer.export_png(record, model, orientation, destination)
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append({"id": record.id, "error": str(exc)})
            continue
        entries.append(
            {
                "id": record.id,
                "patent_number": record.patent_number,
                "simple_name": record.simple_name,
                "source_url": record.source_url,
                "source_artwork": record.local_image_path,
                "verification_status": record.verification_status,
                "model": model,
                "orientation": orientation,
                "output_path": relative_path.as_posix(),
            }
        )

    manifest = {
        "format": "iconic-patents-png-export-v1",
        "library_dir": str(store.library_dir),
        "cache_dir": str(cache_dir) if cache_dir is not None else str(store.library_dir),
        "model": model,
        "orientation": orientation,
        "records": entries,
        "errors": errors,
    }
    manifest_path = destination_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path, entries, errors


def _run_export_png(
    args: argparse.Namespace,
    *,
    renderer_factory: Callable[..., PatentRenderer] = PatentRenderer,
) -> int:
    paths = _resolve_args_paths(args)
    store = PatentStore(paths.library_dir)
    orientation = args.orientation or default_patent_orientation(args.model).value
    manifest_path, entries, errors = export_png_records(
        store,
        output_dir=args.output_dir,
        model=args.model,
        orientation=orientation,
        image_config=paths.config.image_config_default,
        cache_dir=paths.cache_dir,
        renderer_factory=renderer_factory,
        limit=args.limit,
    )
    print(
        json.dumps(
            {
                "exported": len(entries),
                "errors": errors,
                "manifest": str(manifest_path),
                "output_dir": str(args.output_dir),
                "model": args.model,
                "orientation": orientation,
                "config": str(paths.config_path) if paths.config_path else None,
                "library_dir": str(paths.library_dir),
                "cache_dir": str(paths.cache_dir),
            },
            indent=2,
        )
    )
    return 0 if not errors else 2


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "import":
            return _run_import(args)
        if args.command == "export-manifest":
            return _run_export_manifest(args)
        if args.command in {"export-png", "export"}:
            return _run_export_png(args)
    except (CliError, FileNotFoundError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    raise AssertionError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
