"""Per-screen user configuration stored alongside telemetry in serve_scheduler.json."""

from __future__ import annotations

from dataclasses import dataclass

from hokku.webserver.collection_store import ALL_COLLECTION_ID
from hokku.webserver.orientation import Orientation


@dataclass(frozen=True)
class ScreenConfig:
    """Persistent, user-configurable settings for one connected screen.

    ``orientation`` defaults to LANDSCAPE for ordinary image serving. Patent
    rendering can use its model-specific default while this screen remains
    unconfigured. ``orientation_override`` records whether the user explicitly
    selected an orientation, so collection-only updates do not accidentally
    turn the ordinary default into a patent-layout override.

    ``filter_by_orientation``: when True, only images whose native
    orientation matches the screen's orientation are eligible for
    serving. Square (NEUTRAL) images are always eligible regardless.

    ``device_name`` is an optional desired firmware ``X-Screen-Name``; the
    human-facing ``display_name`` remains independent.
    """

    orientation: Orientation = Orientation.LANDSCAPE
    orientation_override: bool | None = None
    filter_by_orientation: bool = False
    server_url_override: str = ""
    active_collection_id: str = ALL_COLLECTION_ID
    display_name: str = ""
    device_name: str = ""

    def __post_init__(self) -> None:
        assert self.orientation in (Orientation.LANDSCAPE, Orientation.PORTRAIT), (
            f"ScreenConfig.orientation must be LANDSCAPE or PORTRAIT, got {self.orientation!r}"
        )
        if self.orientation_override is None:
            object.__setattr__(
                self,
                "orientation_override",
                self.orientation == Orientation.PORTRAIT,
            )

    def to_dict(self) -> dict:
        return {
            "orientation": self.orientation,
            "orientation_override": self.orientation_override,
            "filter_by_orientation": self.filter_by_orientation,
            "server_url_override": self.server_url_override,
            "active_collection_id": self.active_collection_id,
            "display_name": self.display_name,
            "device_name": self.device_name,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ScreenConfig:
        raw_collection_id = d.get("active_collection_id", ALL_COLLECTION_ID)
        return cls(
            orientation=Orientation(d["orientation"]),
            orientation_override=d.get("orientation_override"),
            filter_by_orientation=bool(d.get("filter_by_orientation", False)),
            server_url_override=str(d.get("server_url_override", "")),
            active_collection_id=(
                raw_collection_id
                if isinstance(raw_collection_id, str) and raw_collection_id
                else ALL_COLLECTION_ID
            ),
            display_name=str(d.get("display_name", d.get("screen_label", ""))),
            device_name=str(d.get("device_name", "")),
        )
