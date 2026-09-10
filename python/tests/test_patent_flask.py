"""Flask contract tests for structured Iconic Patents records."""

from __future__ import annotations

import io
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from hokku.webserver.app_config import AppConfig
from hokku.webserver.app_state import AppState, build_manager
from hokku.webserver.flask_app import create_app
from hokku.webserver.image_classifier import ImageClassifier
from hokku.webserver.orientation import Orientation
from hokku.webserver.patent_store import PatentRecord
from hokku.webserver.serve_scheduler import ServeScheduler


def _make_patent_fixture(tmp_path: Path, app_config: AppConfig, fast_image_config):
    config = replace(
        app_config,
        image_config_default=fast_image_config,
        image_config_bw=fast_image_config,
        image_config_face=fast_image_config,
    )
    classifier = ImageClassifier(config)
    manager = build_manager(config, classifier)
    state = AppState(config, classifier, manager, ServeScheduler(manager))

    patent_id = "US1234567B2"
    asset_dir = state.patents.library_dir / "images" / patent_id
    asset_dir.mkdir(parents=True)
    drawing_path = asset_dir / "original.png"
    drawing = Image.new("RGB", (220, 150), "white")
    draw = ImageDraw.Draw(drawing)
    draw.rectangle((20, 20, 200, 125), outline="black", width=4)
    draw.line((20, 125, 200, 20), fill="black", width=3)
    drawing.save(drawing_path)
    (asset_dir / "source.pdf").write_bytes(b"%PDF-iconic-patent")

    bridge_name = "patent-US1234567B2.png"
    state.patents.upsert(
        PatentRecord(
            id=patent_id,
            simple_name="Hydraulic Widget",
            patent_title="Improvement in hydraulic widgets",
            inventor_names=["Ada Lovelace"],
            patent_number=patent_id,
            patent_year=1883,
            description="A compact test patent record.",
            category="Mechanics",
            source_url="https://patents.google.com/patent/US1234567B2/en",
            qr_destination_url="https://patents.google.com/patent/US1234567B2/en",
            local_image_path=f"images/{patent_id}/original.png",
            image_name=bridge_name,
            verification_status="asset_ready",
        )
    )
    shutil.copyfile(drawing_path, Path(config.upload_dir) / bridge_name)
    manager.sync()
    manager.wait_for_idle()
    app = create_app(state, config_path=tmp_path / "config.json", template_folder=None)
    app.config["TESTING"] = True
    return app.test_client(), state, bridge_name, patent_id


def test_patent_routes_expose_metadata_assets_and_render(client_fixture):
    client, _state, bridge_name, patent_id = client_fixture

    listing = client.get("/hokku/api/patents")
    assert listing.status_code == 200
    assert listing.get_json()["patents"][0]["image_name"] == bridge_name

    original = client.get(f"/hokku/api/patent/{patent_id}/original")
    assert original.status_code == 200
    assert original.mimetype == "image/png"

    source_pdf = client.get(f"/hokku/api/patent/{patent_id}/source.pdf")
    assert source_pdf.status_code == 200
    assert source_pdf.mimetype == "application/pdf"
    assert source_pdf.data == b"%PDF-iconic-patent"

    display = client.get(f"/hokku/api/patent/{patent_id}/display")
    assert display.status_code == 200
    assert display.mimetype == "image/png"
    with Image.open(io.BytesIO(display.data)) as preview:
        assert preview.size == (1200, 1600)

    landscape = client.get(f"/hokku/api/patent/{patent_id}/display?orientation=landscape")
    assert landscape.status_code == 200
    with Image.open(io.BytesIO(landscape.data)) as preview:
        assert preview.size == (1600, 1200)

    dithered = client.get(f"/hokku/api/dithered/{bridge_name}")
    assert dithered.status_code == 200
    with Image.open(io.BytesIO(dithered.data)) as preview:
        assert preview.size == (1200, 1600)

    refreshed = client.post(f"/hokku/api/patent/{patent_id}/render")
    assert refreshed.status_code == 200
    assert refreshed.get_json()["patent_id"] == patent_id
    assert refreshed.get_json()["orientation"] == "portrait"

    status = client.get("/hokku/api/status").get_json()
    entry = next(item for item in status["upload_files"] if item["name"] == bridge_name)
    assert entry["item_type"] == "patent"
    assert entry["patent"]["patent_number"] == patent_id

    edited = client.patch(
        f"/hokku/api/patent/{patent_id}",
        json={"description": "Edited from the admin contract test."},
    )
    assert edited.status_code == 200
    assert edited.get_json()["description"] == "Edited from the admin contract test."


def test_screen_route_serves_the_patent_renderer_panel(client_fixture):
    client, state, bridge_name, patent_id = client_fixture

    expected = state.patent_renderer.render(
        state.patents.get(patent_id), "huessen_epf1301", "portrait"
    ).panel_bytes
    response = client.get(
        "/hokku/screen/",
        headers={
            "X-Screen-Name": "patent-screen",
            "X-Screen-Model": "huessen_epf1301",
        },
    )

    assert response.status_code == 200
    assert response.data == expected
    assert state.scheduler.screen_last_served("patent-screen") == bridge_name

    collection_patch = client.patch(
        "/hokku/api/screens/patent-screen/collection",
        json={"collection_id": "all"},
    )
    assert collection_patch.status_code == 200
    assert state.scheduler.get_screen_config("patent-screen").orientation_override is False
    response = client.get(
        "/hokku/screen/",
        headers={
            "X-Screen-Name": "patent-screen",
            "X-Screen-Model": "huessen_epf1301",
        },
    )
    assert response.status_code == 200
    assert response.data == expected

    config_patch = client.patch(
        "/hokku/api/screens/landscape-patent-screen/config",
        json={"orientation": "landscape"},
    )
    assert config_patch.status_code == 200
    explicit_landscape = state.patent_renderer.render(
        state.patents.get(patent_id), "huessen_epf1301", "landscape"
    ).panel_bytes
    response = client.get(
        "/hokku/screen/",
        headers={
            "X-Screen-Name": "landscape-patent-screen",
            "X-Screen-Model": "huessen_epf1301",
        },
    )
    assert response.status_code == 200
    assert response.data == explicit_landscape
    assert state.scheduler.get_screen_config("landscape-patent-screen").orientation_override is True


def test_screen_route_fallback_preserves_patent_default_orientation(client_fixture, monkeypatch):
    client, state, bridge_name, _patent_id = client_fixture

    def fail_render(*_args, **_kwargs):
        raise RuntimeError("synthetic patent render failure")

    monkeypatch.setattr(state.patent_renderer, "render", fail_render)
    expected = state.manager.panel_bytes_for_model_orientation(
        bridge_name, "huessen_epf1301", Orientation.PORTRAIT
    )
    assert expected is not None

    response = client.get(
        "/hokku/screen/",
        headers={
            "X-Screen-Name": "fallback-patent-screen",
            "X-Screen-Model": "huessen_epf1301",
        },
    )

    assert response.status_code == 200
    assert response.data == expected


def test_patent_route_rejects_unknown_record_and_bad_orientation(client_fixture):
    client, _state, _bridge_name, _patent_id = client_fixture

    assert client.get("/hokku/api/patent/missing/display").status_code == 404
    bad = client.get("/hokku/api/patent/US1234567B2/display?orientation=sideways")
    assert bad.status_code == 400


@pytest.fixture
def client_fixture(tmp_path: Path, app_config: AppConfig, fast_image_config):
    client, state, bridge_name, patent_id = _make_patent_fixture(
        tmp_path, app_config, fast_image_config
    )
    yield client, state, bridge_name, patent_id
    state.manager.shutdown()
