"""Public PWA delivery contract, including packaged icon availability."""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from hokku.webserver.app_state import AppState, build_manager
from hokku.webserver.flask_app import create_app
from hokku.webserver.image_classifier import ImageClassifier
from hokku.webserver.serve_scheduler import ServeScheduler


def test_pwa_routes_and_icons(app_config):
    classifier = ImageClassifier(app_config)
    manager = build_manager(app_config, classifier)
    state = AppState(app_config, classifier, manager, ServeScheduler(manager))
    client = create_app(state).test_client()
    worker = client.get("/hokku/service-worker.js")
    assert worker.status_code == 200
    assert "javascript" in worker.content_type
    assert worker.headers["Cache-Control"] == "no-cache"
    manifest = client.get("/hokku/static/manifest.webmanifest")
    assert manifest.status_code == 200
    data = manifest.get_json(force=True)
    assert data["start_url"] == "/hokku/ui"
    assert data["scope"] == "/hokku/"
    for icon in data["icons"]:
        response = client.get(icon["src"])
        assert response.status_code == 200
        with Image.open(io.BytesIO(response.data)) as image:
            assert f"{image.width}x{image.height}" == icon["sizes"]
    offline = client.get("/hokku/static/offline.html")
    assert offline.status_code == 200
    assert "No commands are queued" in offline.get_data(as_text=True)
    # Flask's path guard remains effective for new assets.
    assert client.get("/hokku/static/../app_config.py").status_code == 404


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize("filename", ["mobile.js", "service-worker.js"])
def test_pwa_javascript_parses(filename):
    asset = Path(__file__).parents[1] / "hokku" / "webserver" / "static" / filename
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run(
        [node, "--check", str(asset)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
