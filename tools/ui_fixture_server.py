"""Read-only dashboard fixture for browser QA; never connects to a frame.

Run with PYTHONPATH=python python tools/ui_fixture_server.py --port 18084.
All data is synthetic. Every non-GET request is rejected before route handling.
"""

from __future__ import annotations

import argparse
import tempfile
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Response, jsonify, request

from hokku.webserver.app_config import AppConfig
from hokku.webserver.app_state import AppState, build_manager
from hokku.webserver.flask_app import create_app
from hokku.webserver.image_classifier import ImageClassifier
from hokku.webserver.serve_scheduler import ServeScheduler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18084)
    parser.add_argument("--template", type=Path)
    parser.add_argument("--worker-version-file", type=Path, help="Fixture-only update simulation")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="hokku-ui-fixture-") as directory:
        root = Path(directory)
        (root / "uploads").mkdir()
        (root / "cache").mkdir()
        cfg = AppConfig(upload_dir=str(root / "uploads"), cache_dir=str(root / "cache"))
        classifier = ImageClassifier(cfg)
        manager = build_manager(cfg, classifier)
        state = AppState(cfg, classifier, manager, ServeScheduler(manager))
        app = create_app(state)
        app.jinja_env.auto_reload = True
        base_status = create_app(state).test_client().get("/hokku/api/status").get_json()
        files = [
            {
                "name": f"Fixture_landscape_{i:02d}.png",
                "dithered": True,
                "status": "ok",
                "collection_ids": ["sample"],
                "image_width": 960,
                "image_height": 640,
            }
            for i in range(1, 24)
        ]
        collections = [
            {"id": "all", "name": "All Photos", "image_count": len(files)},
            {"id": "sample", "name": "Weekend landscapes — fixture", "image_count": len(files)},
            {"id": "empty", "name": "Empty collection — fixture", "image_count": 0},
        ]
        status = {
            **base_status,
            "pool_size": len(files),
            "upload_size": len(files),
            "upload_files": files,
            "pool_files": [f["name"] for f in files],
            "collections": collections,
            "screens": {
                "demo-frame": {
                    "display_name": "Demo frame · fixture",
                    "screen_model": "huessen_epf1301",
                    "ip": "192.0.2.40",
                    "battery_mv": 3880,
                    "battery_percent": 73,
                    "last_seen": "Fixture timestamp",
                    "next_update_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                    "last_served": files[2]["name"],
                    "active_collection_id": "sample",
                    "orientation": "landscape",
                    "state": {"fw": "fixture"},
                }
            },
            "converting": 0,
            "failed_files": [],
            "last_served": "Fixture_landscape_03.png",
        }

        @app.before_request
        def fixture_guard():
            if request.method not in {"GET", "HEAD"}:
                return jsonify(error="Read-only fixture: actions are disabled."), 405
            if request.path == "/hokku/api/status":
                return jsonify(status)
            if request.path.startswith("/hokku/api/image/") and request.path.endswith("/config"):
                return jsonify(
                    overrides={"image_config": None, "crop_to_fill_threshold": None},
                    effective={
                        "image_config": asdict(cfg.image_config_default),
                        "crop_to_fill_threshold": cfg.crop_to_fill_threshold,
                    },
                    pipeline="default",
                )
            if args.worker_version_file and request.path == "/hokku/service-worker.js":
                script = (
                    Path(__file__).resolve().parents[1]
                    / "python/hokku/webserver/static/service-worker.js"
                ).read_text()
                script = script.replace(
                    "hokku-public-v1", args.worker_version_file.read_text().strip()
                )
                return Response(
                    script, mimetype="text/javascript", headers={"Cache-Control": "no-cache"}
                )
            if request.path.startswith(("/hokku/api/thumbnail/", "/hokku/api/original/")):
                return Response(
                    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 640">'
                    '<rect width="960" height="640" fill="#d9e3dc"/>'
                    '<path d="M0 440 240 170 530 490 700 280 960 540V640H0Z" fill="#63796b"/>'
                    '<text x="40" y="600" fill="white" font-size="32">FIXTURE IMAGE</text></svg>',
                    mimetype="image/svg+xml",
                )
            if args.template and request.path == "/hokku/ui":
                from flask import render_template_string  # noqa: PLC0415

                return render_template_string(
                    args.template.read_text(encoding="utf-8"), visual_w=1600, visual_h=1200
                )
            return None

        @app.after_request
        def label_fixture(response):
            if request.path == "/hokku/ui":
                response.set_data(
                    response.get_data(as_text=True).replace(
                        "<body>",
                        '<body><div style="padding:8px 16px;background:#2e4f6e;color:white;'
                        'text-align:center;font:14px system-ui">Browser QA · Synthetic fixture data · '
                        "All write actions disabled</div>",
                        1,
                    )
                )
            return response

        app.run(host="127.0.0.1", port=args.port, use_reloader=False)


if __name__ == "__main__":
    main()
