"""Compose and render patent cards through Hokku's normal image pipeline.

The composition in this module is deliberately a small presentation layer.  It
keeps the source drawing as pixels, adds restrained black-on-white metadata and
a QR code, then hands the resulting RGB image to :class:`ImageRenderer` for
the display-specific fit, enhancement, dithering, and wire packing.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import qrcode
import zstd
from PIL import Image, ImageDraw, ImageFont, ImageOps

from hokku.screens.display import Display
from hokku.screens.registry import DISPLAY_REGISTRY
from hokku.webserver.dither_streaming_numba import NumbaStreamingDither
from hokku.webserver.image_abc import preview_png_from_panel_bytes
from hokku.webserver.image_config import ImageConfig
from hokku.webserver.image_renderer import ImageRenderer, open_image_for_render
from hokku.webserver.orientation import Orientation

if TYPE_CHECKING:
    from hokku.webserver.patent_store import PatentRecord


_WHITE = (255, 255, 255)
_BLACK = (0, 0, 0)
_PORTRAIT_DISPLAY_MODEL = "huessen_epf1301"
_PANEL_SUFFIX = "_panel.bin.zst"
_PREVIEW_SUFFIX = "_preview.png"
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")

# The project ships a webfont in WOFF2 form, which PIL cannot use directly.
# These paths cover the standard fonts on macOS and Debian/Ubuntu images.
_FONT_PATHS = {
    False: (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ),
    True: (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ),
}


@dataclass(frozen=True)
class PatentRenderResult:
    """The two cached render products and the deterministic cache key."""

    panel_bytes: bytes
    preview_bytes: bytes
    cache_key: str
    cache_hit: bool = False


def _as_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _inventor_text(names: object) -> str:
    if isinstance(names, str):
        values = [part.strip() for part in names.split(",") if part.strip()]
    else:
        try:
            values = [_as_text(name) for name in names]  # type: ignore[union-attr]
        except TypeError:
            values = [_as_text(names)]
        values = [value for value in values if value]
    return ", ".join(values)


def _safe_id(value: object) -> str:
    clean = _SAFE_ID_RE.sub("-", _as_text(value)).strip("-._")
    return clean[:48] or "patent"


def _source_fingerprint(path: Path) -> str:
    """Hash the source bytes so replacing an art file invalidates its render."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return "missing"
    return digest.hexdigest()


def _font(bold: bool, size: int):
    size = max(1, int(size))
    for candidate in _FONT_PATHS[bold]:
        path = Path(candidate)
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _line_height(draw: ImageDraw.ImageDraw, font) -> int:
    box = draw.textbbox((0, 0), "Ag", font=font)
    return max(1, box[3] - box[1])


def _split_long_word(draw: ImageDraw.ImageDraw, word: str, font, max_width: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for character in word:
        candidate = current + character
        if current and _text_width(draw, candidate, font) > max_width:
            chunks.append(current)
            current = character
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [word]


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    """Wrap on words, splitting an unusually long token when necessary."""
    lines: list[str] = []
    paragraphs = text.splitlines() or [""]
    for paragraph in paragraphs:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = ""
        for word in words:
            if _text_width(draw, word, font) > max_width:
                if current:
                    lines.append(current)
                    current = ""
                pieces = _split_long_word(draw, word, font, max_width)
                lines.extend(pieces[:-1])
                current = pieces[-1]
                continue
            candidate = word if not current else f"{current} {word}"
            if current and _text_width(draw, candidate, font) > max_width:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
    return lines or [""]


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    max_height: int,
    base_size: int,
    min_size: int,
    bold: bool,
) -> tuple[object, list[str]]:
    """Choose the largest readable font that fits the available text block."""
    max_width = max(1, max_width)
    max_height = max(1, max_height)
    for size in range(max(base_size, min_size), min_size - 1, -1):
        candidate = _font(bold, size)
        lines = _wrap_text(draw, text, candidate, max_width)
        spacing = max(2, size // 5)
        height = len(lines) * _line_height(draw, candidate) + max(0, len(lines) - 1) * spacing
        if height <= max_height:
            return candidate, lines

    candidate = _font(bold, min_size)
    lines = _wrap_text(draw, text, candidate, max_width)
    spacing = max(2, min_size // 5)
    max_lines = max(1, (max_height + spacing) // (_line_height(draw, candidate) + spacing))
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1].rstrip()
        while last and _text_width(draw, f"{last}…", candidate) > max_width:
            last = last[:-1].rstrip()
        lines[-1] = f"{last}…" if last else "…"
    return candidate, lines


def _draw_lines(
    draw: ImageDraw.ImageDraw,
    lines: Iterable[str],
    font,
    x: int,
    y: int,
    *,
    fill: tuple[int, int, int] = _BLACK,
) -> int:
    lines = list(lines)
    size = max(1, getattr(font, "size", 12))
    spacing = max(2, size // 5)
    height = _line_height(draw, font)
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += height + spacing
    return y - spacing


def default_patent_orientation(model: str) -> Orientation:
    """Return the approved default orientation for a patent display model.

    The Huessen EPF1301 is the 13.3-inch Spectra 6 target whose physical
    patent card is 7.8 inches wide by 10.6 inches high. Other models retain
    the existing landscape default until they receive their own layout.
    Callers can still pass an explicit orientation to override this choice.
    """

    return (
        Orientation.PORTRAIT
        if _as_text(model) == _PORTRAIT_DISPLAY_MODEL
        else Orientation.LANDSCAPE
    )


def resolve_patent_orientation(model: str, orientation: Orientation | str | None) -> Orientation:
    """Resolve an optional orientation without duplicating model defaults."""

    if orientation is None or not _as_text(orientation):
        return default_patent_orientation(model)
    try:
        resolved = Orientation(orientation)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"orientation must be 'landscape' or 'portrait', got {orientation!r}"
        ) from exc
    if resolved not in (Orientation.LANDSCAPE, Orientation.PORTRAIT):
        raise ValueError(f"orientation must be 'landscape' or 'portrait', got {orientation!r}")
    return resolved


def _visible_size(display: Display, orientation: Orientation) -> tuple[int, int]:
    """Return the dimensions a viewer sees before panel-memory rotation."""
    if not display.panel_rotated:
        return display.panel_w, display.panel_h
    if orientation == Orientation.LANDSCAPE:
        return display.panel_h, display.panel_w
    return display.panel_w, display.panel_h


class PatentRenderer:
    """Render a patent drawing and metadata card for any registered display."""

    def __init__(
        self,
        library_dir: str | Path,
        image_config: ImageConfig,
        template_version: str = "patent-v1",
        cache_dir: str | Path | None = None,
    ) -> None:
        self.library_dir = Path(library_dir).resolve()
        self.image_config = image_config
        self.template_version = _as_text(template_version) or "patent-v1"
        self.cache_dir = (Path(cache_dir) if cache_dir is not None else self.library_dir).resolve()
        self._renders_dir = self.cache_dir / "renders"

    def _display(self, model: str) -> Display:
        try:
            return DISPLAY_REGISTRY[model]
        except KeyError as exc:
            raise ValueError(f"Unknown display model {model!r}") from exc

    def _source_path(self, record: PatentRecord) -> Path:
        raw_path = _as_text(record.local_image_path)
        if not raw_path:
            raise ValueError("PatentRecord.local_image_path is required for rendering")
        path = Path(raw_path)
        return path if path.is_absolute() else self.library_dir / path

    @staticmethod
    def _validate_qr_url(url: object) -> str:
        value = _as_text(url)
        parsed = urlparse(value)
        if not value or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                "PatentRecord.qr_destination_url must be a valid http(s) URL for QR generation"
            )
        return value

    def _cache_key(self, record: PatentRecord, display: Display, orientation: Orientation) -> str:
        source_path = self._source_path(record)
        payload = {
            "id": _as_text(record.id),
            "local_image_path": str(source_path),
            "image_fingerprint": _source_fingerprint(source_path),
            "qr_destination_url": _as_text(record.qr_destination_url),
            "simple_name": _as_text(record.simple_name),
            "description": _as_text(record.description),
            "inventor_names": _inventor_text(record.inventor_names),
            "patent_year": _as_text(record.patent_year),
            "patent_number": _as_text(record.patent_number),
            "template_version": self.template_version,
            "model": display.model_id,
            "panel_dimensions": [display.panel_w, display.panel_h],
            "visible_dimensions": [display.visual_w, display.visual_h],
            "panel_rotated": display.panel_rotated,
            "orientation": orientation.value,
            "image_config": self.image_config.cache_slug(),
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        record_token = hashlib.sha256(_as_text(record.id).encode("utf-8")).hexdigest()[:12]
        return f"{_safe_id(record.id)}-{record_token}-{digest[:32]}"

    def _compose_portrait(
        self, record: PatentRecord, display: Display, orientation: Orientation
    ) -> Image.Image:
        """Compose the portrait 13.3-inch Spectra 6 reference layout.

        The physical panel is 7.8 inches wide by 10.6 inches high, which maps
        to the Huessen's 1200×1600 portrait view.  The artwork gets the left
        two-thirds of the panel; the right rail holds readable metadata,
        description, and the patent QR code.
        """
        url = self._validate_qr_url(record.qr_destination_url)
        canvas_w, canvas_h = _visible_size(display, orientation)
        canvas = Image.new("RGB", (canvas_w, canvas_h), _WHITE)
        draw = ImageDraw.Draw(canvas)

        minimum = min(canvas_w, canvas_h)
        rule_width = max(1, round(minimum / 600))
        margin = max(10, round(minimum * 0.012))
        divider_x = round(canvas_w * 0.667)
        divider_gap = max(14, round(minimum * 0.02))
        art_left = margin + round(minimum * 0.025)
        art_top = margin + round(canvas_h * 0.035)
        art_right = divider_x - divider_gap
        art_bottom = canvas_h - margin - round(canvas_h * 0.035)

        # The sample has a quiet keyline around the whole physical card and a
        # divider that stops short of the outer border.
        draw.rectangle(
            (
                rule_width // 2,
                rule_width // 2,
                canvas_w - 1 - rule_width // 2,
                canvas_h - 1 - rule_width // 2,
            ),
            outline=_BLACK,
            width=rule_width,
        )
        draw.line(
            (divider_x, art_top, divider_x, art_bottom),
            fill=_BLACK,
            width=rule_width,
        )

        source_path = self._source_path(record)
        with open_image_for_render(source_path) as source:
            # Portrait cards should use the available height.  A contained
            # page leaves the drawing postage-stamp sized on this tall panel;
            # crop-to-fill removes only the excess side whitespace while
            # preserving the original drawing pixels.
            fitted = ImageOps.fit(
                source,
                (max(1, art_right - art_left), max(1, art_bottom - art_top)),
                method=Image.Resampling.LANCZOS,
                centering=(0.48, 0.5),
            )
            canvas.paste(fitted, (art_left, art_top))
            fitted.close()

        rail_left = divider_x + round(minimum * 0.03)
        rail_right = canvas_w - margin
        rail_top = margin + round(minimum * 0.018)
        rail_bottom = canvas_h - margin - round(minimum * 0.018)
        rail_width = max(1, rail_right - rail_left)
        rail_height = max(1, rail_bottom - rail_top)
        rule_one = rail_top + round(rail_height * 0.21)
        rule_two = rail_top + round(rail_height * 0.43)
        rule_three = rail_top + round(rail_height * 0.72)

        title_font, title_lines = _fit_text(
            draw,
            _as_text(record.simple_name) or "Untitled patent",
            rail_width,
            max(1, rule_one - rail_top - 24),
            max(18, round(minimum * 0.055)),
            max(14, round(minimum * 0.028)),
            True,
        )
        _draw_lines(draw, title_lines, title_font, rail_left, rail_top)
        for y in (rule_one, rule_two, rule_three):
            draw.line((rail_left, y, rail_right, y), fill=_BLACK, width=rule_width)

        cursor = rule_one + round(minimum * 0.028)
        body_size = max(18, round(minimum * 0.029))
        label_font = _font(False, body_size)
        body_font = _font(False, body_size)
        inventors = _inventor_text(record.inventor_names)
        if inventors:
            cursor = _draw_lines(draw, ["Inventor:"], label_font, rail_left, cursor)
            cursor += round(minimum * 0.008)
            inventor_font, inventor_lines = _fit_text(
                draw,
                inventors,
                rail_width,
                max(1, rule_two - cursor - round(minimum * 0.045)),
                body_size,
                max(16, round(minimum * 0.022)),
                False,
            )
            cursor = _draw_lines(draw, inventor_lines, inventor_font, rail_left, cursor)
            cursor += round(minimum * 0.022)
        if _as_text(record.patent_year):
            cursor = _draw_lines(draw, [_as_text(record.patent_year)], body_font, rail_left, cursor)
            cursor += round(minimum * 0.014)
        if _as_text(record.patent_number):
            cursor = _draw_lines(
                draw,
                [f"Patent {_as_text(record.patent_number)}"],
                body_font,
                rail_left,
                cursor,
            )

        description = _as_text(record.description)
        if description:
            description_font, description_lines = _fit_text(
                draw,
                description,
                rail_width,
                max(1, rule_three - rule_two - round(minimum * 0.065)),
                max(18, round(minimum * 0.026)),
                max(15, round(minimum * 0.019)),
                False,
            )
            _draw_lines(
                draw,
                description_lines,
                description_font,
                rail_left,
                rule_two + round(minimum * 0.05),
            )

        qr_size = min(
            round(minimum * 0.205),
            rail_width,
            max(72, rail_bottom - rule_three - round(minimum * 0.08)),
        )
        qr_x = rail_left + (rail_width - qr_size) // 2
        qr_y = rule_three + round(minimum * 0.055)
        qr_image = self._qr_image(url, qr_size)
        canvas.paste(qr_image, (qr_x, qr_y))
        qr_image.close()
        caption_font = _font(False, max(14, round(minimum * 0.017)))
        caption = "V I E W   P A T E N T"
        caption_width = _text_width(draw, caption, caption_font)
        draw.text(
            (rail_left + (rail_width - caption_width) // 2, qr_y + qr_size + round(minimum * 0.02)),
            caption,
            font=caption_font,
            fill=_BLACK,
        )
        return canvas

    def _cache_paths(
        self, model: str, orientation: Orientation, cache_key: str
    ) -> tuple[Path, Path]:
        directory = self._renders_dir / model / orientation.value
        return (
            directory / f"{cache_key}{_PANEL_SUFFIX}",
            directory / f"{cache_key}{_PREVIEW_SUFFIX}",
        )

    @staticmethod
    def _valid_preview(data: bytes, expected_size: tuple[int, int]) -> bool:
        try:
            with Image.open(BytesIO(data)) as image:
                image.verify()
            with Image.open(BytesIO(data)) as image:
                return image.size == expected_size
        except (OSError, ValueError):
            return False

    def _cache_hit(
        self,
        panel_path: Path,
        preview_path: Path,
        display: Display,
        orientation: Orientation,
    ) -> tuple[bytes, bytes] | None:
        if not panel_path.is_file() or not preview_path.is_file():
            return None
        try:
            panel_bytes = zstd.decompress(panel_path.read_bytes())
            preview_bytes = preview_path.read_bytes()
        except Exception:
            return None
        if len(panel_bytes) != display.total_bytes:
            return None
        if not self._valid_preview(preview_bytes, _visible_size(display, orientation)):
            return None
        return panel_bytes, preview_bytes

    @staticmethod
    def _qr_image(url: str, size: int) -> Image.Image:
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=1,
            border=4,
        )
        qr.add_data(url)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        if image.size != (size, size):
            image = image.resize((size, size), Image.Resampling.NEAREST)
        return image

    def _compose(
        self, record: PatentRecord, display: Display, orientation: Orientation
    ) -> Image.Image:
        visible_w, visible_h = _visible_size(display, orientation)
        if orientation == Orientation.PORTRAIT and visible_h > visible_w:
            return self._compose_portrait(record, display, orientation)

        url = self._validate_qr_url(record.qr_destination_url)
        canvas_w, canvas_h = _visible_size(display, orientation)
        canvas = Image.new("RGB", (canvas_w, canvas_h), _WHITE)
        draw = ImageDraw.Draw(canvas)

        minimum = min(canvas_w, canvas_h)
        margin = max(8, int(minimum * 0.045))
        gap = max(8, int(minimum * 0.035))
        usable_w = canvas_w - 2 * margin
        usable_h = canvas_h - 2 * margin

        if canvas_w >= canvas_h:
            metadata_w = max(120, int(usable_w * 0.22))
            metadata_w = min(metadata_w, max(100, usable_w // 3))
            art_box = (margin, margin, usable_w - metadata_w - gap, usable_h)
            metadata_box = (
                margin + art_box[2] + gap,
                margin,
                metadata_w,
                usable_h,
            )
            qr_size = min(int(minimum * 0.24), metadata_w - 2 * max(6, int(minimum * 0.02)))
            qr_size = max(72, qr_size)
            qr_x = metadata_box[0] + (metadata_w - qr_size) // 2
            qr_y = metadata_box[1] + metadata_box[3] - qr_size
            text_box = (
                metadata_box[0] + max(6, int(minimum * 0.02)),
                metadata_box[1] + max(6, int(minimum * 0.02)),
                metadata_w - 2 * max(6, int(minimum * 0.02)),
                max(1, qr_y - metadata_box[1] - gap),
            )
        else:
            metadata_h = max(140, int(usable_h * 0.22))
            metadata_h = min(metadata_h, max(120, usable_h // 3))
            art_box = (margin, margin, usable_w, usable_h - metadata_h - gap)
            metadata_box = (
                margin,
                margin + art_box[3] + gap,
                usable_w,
                metadata_h,
            )
            pad = max(6, int(minimum * 0.02))
            qr_size = min(int(minimum * 0.24), metadata_h - 2 * pad)
            qr_size = max(72, qr_size)
            qr_x = metadata_box[0] + metadata_box[2] - qr_size - pad
            qr_y = metadata_box[1] + (metadata_h - qr_size) // 2
            text_box = (
                metadata_box[0] + pad,
                metadata_box[1] + pad,
                max(1, qr_x - metadata_box[0] - pad - gap),
                metadata_h - 2 * pad,
            )

        # Keep the original drawing intact apart from a proportional contain fit.
        source_path = self._source_path(record)
        with open_image_for_render(source_path) as source:
            fitted = ImageOps.contain(
                source,
                (max(1, art_box[2]), max(1, art_box[3])),
                method=Image.Resampling.LANCZOS,
            )
            art_x = art_box[0] + (art_box[2] - fitted.width) // 2
            art_y = art_box[1] + (art_box[3] - fitted.height) // 2
            canvas.paste(fitted, (art_x, art_y))
            fitted.close()

        text_x, text_y, text_w, text_h = text_box
        title = _as_text(record.simple_name) or "Untitled patent"
        title_font, title_lines = _fit_text(
            draw,
            title,
            text_w,
            max(1, int(text_h * 0.43)),
            max(16, int(minimum * 0.055)),
            max(12, int(minimum * 0.025)),
            True,
        )
        cursor = _draw_lines(draw, title_lines, title_font, text_x, text_y)
        cursor += max(6, int(minimum * 0.018))

        inventors = _inventor_text(record.inventor_names)
        if inventors:
            inventor_font, inventor_lines = _fit_text(
                draw,
                f"Inventors: {inventors}",
                text_w,
                max(1, int(text_h * 0.32)),
                max(12, int(minimum * 0.029)),
                max(10, int(minimum * 0.018)),
                False,
            )
            cursor = _draw_lines(draw, inventor_lines, inventor_font, text_x, cursor)
            cursor += max(4, int(minimum * 0.012))

        year = _as_text(record.patent_year)
        if year:
            year_font = _font(False, max(11, int(minimum * 0.025)))
            cursor = _draw_lines(draw, [year], year_font, text_x, cursor)
            cursor += max(3, int(minimum * 0.008))

        patent_number = _as_text(record.patent_number)
        if patent_number:
            number_font = _font(False, max(10, int(minimum * 0.021)))
            number_lines = _wrap_text(draw, f"Patent {patent_number}", number_font, text_w)
            _draw_lines(draw, number_lines, number_font, text_x, cursor)

        qr_image = self._qr_image(url, qr_size)
        canvas.paste(qr_image, (qr_x, qr_y))
        qr_image.close()
        return canvas

    def render(
        self,
        record: PatentRecord,
        model: str,
        orientation: Orientation | str | None,
    ) -> PatentRenderResult:
        """Render *record*, reading/writing the model-and-orientation cache."""
        display = self._display(model)
        normalized_orientation = resolve_patent_orientation(model, orientation)
        # Validate before a cache lookup so a malformed record can never appear
        # valid merely because an unrelated older cache file exists.
        self._validate_qr_url(record.qr_destination_url)
        cache_key = self._cache_key(record, display, normalized_orientation)
        panel_path, preview_path = self._cache_paths(model, normalized_orientation, cache_key)
        cached = self._cache_hit(panel_path, preview_path, display, normalized_orientation)
        if cached is not None:
            return PatentRenderResult(cached[0], cached[1], cache_key, cache_hit=True)

        composite = self._compose(record, display, normalized_orientation)
        try:
            renderer = ImageRenderer(NumbaStreamingDither(display), display)
            panel_bytes = renderer.render_panel_bytes(
                composite,
                self.image_config,
                normalized_orientation,
            )
        finally:
            composite.close()
        preview_bytes = preview_png_from_panel_bytes(
            panel_bytes,
            normalized_orientation,
            display,
        )

        panel_path.parent.mkdir(parents=True, exist_ok=True)
        panel_path.write_bytes(zstd.compress(panel_bytes, 1))
        preview_path.write_bytes(preview_bytes)
        return PatentRenderResult(panel_bytes, preview_bytes, cache_key, cache_hit=False)

    def render_preview_png(
        self,
        record: PatentRecord,
        model: str,
        orientation: Orientation | str | None,
    ) -> bytes:
        """Return the full visible-size PNG, reusing the panel render cache."""
        return self.render(record, model, orientation).preview_bytes

    def invalidate(self, record: PatentRecord) -> None:
        """Remove every cached model/orientation render for this patent id."""
        record_token = hashlib.sha256(_as_text(record.id).encode("utf-8")).hexdigest()[:12]
        if not self._renders_dir.exists():
            return
        for panel_path in self._renders_dir.rglob(f"*-{record_token}-*{_PANEL_SUFFIX}"):
            preview_path = panel_path.with_name(
                panel_path.name[: -len(_PANEL_SUFFIX)] + _PREVIEW_SUFFIX
            )
            try:
                panel_path.unlink()
            except FileNotFoundError:
                pass
            try:
                preview_path.unlink()
            except FileNotFoundError:
                pass

    def export_png(
        self,
        record: PatentRecord,
        model: str,
        orientation: Orientation | str | None,
        destination: str | Path,
    ) -> Path:
        """Write the visible-size PNG to *destination* and return its path."""
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.render_preview_png(record, model, orientation))
        return path
