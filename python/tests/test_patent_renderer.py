"""Focused tests for the iconic patent composition and render cache."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw

from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.orientation import Orientation
from hokku.webserver.patent_renderer import (
    PatentRenderer,
    default_patent_orientation,
)


@dataclass(frozen=True)
class _Record:
    id: str
    local_image_path: Path
    qr_destination_url: str | None
    simple_name: str
    description: str | None
    inventor_names: tuple[str, ...]
    patent_year: int
    patent_number: str


def _record(
    tmp_path: Path,
    *,
    name: str = "Hydraulic Widget",
    url: str | None = "https://example.com/patent/42",
) -> _Record:
    drawing_path = tmp_path / "drawing.png"
    drawing = Image.new("RGB", (180, 120), "white")
    pen = ImageDraw.Draw(drawing)
    pen.rectangle((18, 18, 162, 102), outline="black", width=4)
    pen.line((18, 102, 162, 18), fill="black", width=3)
    drawing.save(drawing_path)
    return _Record(
        id="patent-42",
        local_image_path=drawing_path,
        qr_destination_url=url,
        simple_name=name,
        description="A practical machine for forming a locked thread stitch.",
        inventor_names=("Ada Lovelace", "Grace Hopper"),
        patent_year=1883,
        patent_number="US-42-A",
    )


def test_cache_key_changes_when_metadata_changes(tmp_path: Path, fast_image_config) -> None:
    renderer = PatentRenderer(tmp_path / "library", fast_image_config)
    base = _record(tmp_path)
    changed = _record(tmp_path, name="Improved Hydraulic Widget")

    first = renderer.render(base, "bigme_f7", Orientation.LANDSCAPE)
    second = renderer.render(changed, "bigme_f7", Orientation.LANDSCAPE)

    assert first.cache_key != second.cache_key


def test_first_render_caches_preview_and_qr_then_second_is_a_hit(
    tmp_path: Path, fast_image_config
) -> None:
    renderer = PatentRenderer(tmp_path / "library", fast_image_config)
    record = _record(tmp_path)

    first = renderer.render(record, "bigme_f7", Orientation.LANDSCAPE)
    second = renderer.render(record, "bigme_f7", Orientation.LANDSCAPE)

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert first.panel_bytes
    assert first.preview_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(first.preview_bytes) > 200
    assert first.panel_bytes == second.panel_bytes
    assert first.preview_bytes == second.preview_bytes

    with Image.open(BytesIO(first.preview_bytes)) as preview:
        decoded, points, _ = cv2.QRCodeDetector().detectAndDecode(
            np.asarray(preview.convert("RGB"))
        )
    assert decoded == record.qr_destination_url
    assert points is not None

    cache_dir = tmp_path / "library" / "renders" / "bigme_f7" / "landscape"
    assert list(cache_dir.glob(f"{first.cache_key}*.zst"))
    assert list(cache_dir.glob(f"{first.cache_key}*.png"))

    renderer.invalidate(record)
    after_invalidate = renderer.render(record, "bigme_f7", Orientation.LANDSCAPE)
    assert after_invalidate.cache_hit is False


def test_export_writes_full_display_size_png(tmp_path: Path, fast_image_config) -> None:
    renderer = PatentRenderer(tmp_path / "library", fast_image_config)
    destination = tmp_path / "exports" / "patent.png"

    renderer.export_png(_record(tmp_path), "bigme_f7", Orientation.LANDSCAPE, destination)

    with Image.open(destination) as exported:
        assert exported.size == (
            DISPLAY_REGISTRY["bigme_f7"].visual_w,
            DISPLAY_REGISTRY["bigme_f7"].visual_h,
        )
        assert exported.mode == "RGB"


def test_rotated_display_preview_uses_visible_orientation(
    tmp_path: Path, fast_image_config
) -> None:
    renderer = PatentRenderer(tmp_path / "library", fast_image_config)
    record = _record(tmp_path)

    landscape = renderer.render(record, "huessen_epf1301", Orientation.LANDSCAPE)
    portrait = renderer.render(record, "huessen_epf1301", Orientation.PORTRAIT)

    with Image.open(BytesIO(landscape.preview_bytes)) as image:
        assert image.size == (1600, 1200)
    with Image.open(BytesIO(portrait.preview_bytes)) as image:
        assert image.size == (1200, 1600)


def test_model_defaults_choose_portrait_only_for_the_spectra_target(
    tmp_path: Path, fast_image_config
) -> None:
    assert default_patent_orientation("huessen_epf1301") == Orientation.PORTRAIT
    assert default_patent_orientation("bigme_f7") == Orientation.LANDSCAPE

    renderer = PatentRenderer(tmp_path / "library", fast_image_config)
    record = _record(tmp_path)

    huessen_default = renderer.render(record, "huessen_epf1301", None)
    bigme_default = renderer.render(record, "bigme_f7", None)

    with Image.open(BytesIO(huessen_default.preview_bytes)) as huessen_preview:
        assert huessen_preview.size == (1200, 1600)
    with Image.open(BytesIO(bigme_default.preview_bytes)) as bigme_preview:
        assert bigme_preview.size == (
            DISPLAY_REGISTRY["bigme_f7"].visual_w,
            DISPLAY_REGISTRY["bigme_f7"].visual_h,
        )


def test_portrait_patent_layout_uses_tall_card_with_side_rail(
    tmp_path: Path, fast_image_config
) -> None:
    renderer = PatentRenderer(tmp_path / "library", fast_image_config)
    record = _record(tmp_path, name="Sewing Machine")

    portrait = renderer.render(record, "huessen_epf1301", Orientation.PORTRAIT)

    with Image.open(BytesIO(portrait.preview_bytes)) as image:
        assert image.size == (1200, 1600)
        pixels = np.asarray(image.convert("L"))
        # The portrait reference card has a strong vertical divider at about
        # two-thirds width and a keyline around the physical panel.
        assert (pixels[:, 799:802] < 80).sum() > 700
        assert (pixels[0:3, :] < 80).sum() > 800

    decoded, points, _ = cv2.QRCodeDetector().detectAndDecode(
        np.asarray(Image.open(BytesIO(portrait.preview_bytes)).convert("RGB"))
    )
    assert decoded == record.qr_destination_url
    assert points is not None


@pytest.mark.parametrize("url", [None, "", "not-a-url", "ftp://example.com/patent/42"])
def test_invalid_or_missing_qr_url_is_rejected(
    tmp_path: Path, fast_image_config, url: str | None
) -> None:
    renderer = PatentRenderer(tmp_path / "library", fast_image_config)

    with pytest.raises(ValueError, match="qr_destination_url"):
        renderer.render(_record(tmp_path, url=url), "bigme_f7", Orientation.LANDSCAPE)
