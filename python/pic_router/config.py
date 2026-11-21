"""Configuration, generic photonic cross-section presets, user JSON persistence, and KLayout DB loader.

Pure Python 3.10+ stdlib + pya / klayout.db (zero numpy/scipy dependencies).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import sys
from typing import Any

DEFAULT_PIN_LAYER: tuple[int, int] = (90, 0)
DEFAULT_GUIDE_LAYER: tuple[int, int] = (90, 1)
DEFAULT_LENGTH_LAYER: tuple[int, int] = (90, 2)
DEFAULT_ERROR_LAYER: tuple[int, int] = (90, 99)
DEFAULT_LENGTH_LAYER_COLOR: int = 0x0F1C4D  # Deep dark navy (high contrast on white canvas)
DEFAULT_SHIFT_STEP_UM: float = 1.0  # Minimum 1 um step when Shift-routing / free-drawing
DEFAULT_DBU: float = 0.001

DEFAULT_PRESET_NAME: str = "Core + Trench (Rib)"

VALID_LAYER_ROLES: frozenset[str] = frozenset(
    {"core", "trench", "cladding", "keepout"}
)


def get_kdb() -> Any:
    """Return KLayout DB module (`pya` inside KLayout, else `klayout.db`)."""
    try:
        import pya as kdb  # type: ignore[import-not-found]
        return kdb
    except ImportError:
        import klayout.db as kdb  # type: ignore[import-not-found]
        return kdb


@dataclass
class LayerSpec:
    """Single cross-section layer specification."""

    layer: int
    datatype: int
    width: float
    offset: float = 0.0
    role: str = "core"

    def __post_init__(self) -> None:
        self.layer = int(self.layer)
        self.datatype = int(self.datatype)
        self.width = float(self.width)
        self.offset = float(self.offset)
        if self.role not in VALID_LAYER_ROLES:
            raise ValueError(
                f"Invalid layer role {self.role!r}; must be one of {sorted(VALID_LAYER_ROLES)}"
            )
        if self.width < 0.0:
            raise ValueError(f"LayerSpec width must be >= 0, got {self.width}")

    @property
    def layer_pair(self) -> tuple[int, int]:
        return (self.layer, self.datatype)

    def copy(self) -> LayerSpec:
        return LayerSpec(
            layer=self.layer,
            datatype=self.datatype,
            width=self.width,
            offset=self.offset,
            role=self.role,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "datatype": self.datatype,
            "width": self.width,
            "offset": self.offset,
            "role": self.role,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LayerSpec:
        return cls(
            layer=int(data["layer"]),
            datatype=int(data["datatype"]),
            width=float(data["width"]),
            offset=float(data.get("offset", 0.0)),
            role=str(data.get("role", "core")),
        )


@dataclass
class CrossSection:
    """Multi-layer photonic waveguide cross-section and bend parameters."""

    name: str
    layers: list[LayerSpec] = field(default_factory=list)
    radius: float = 50.0
    euler_p: float = 0.5
    min_straight: float = 5.0

    def __post_init__(self) -> None:
        self.radius = float(self.radius)
        self.euler_p = float(self.euler_p)
        self.min_straight = float(self.min_straight)
        if self.radius <= 0.0:
            raise ValueError(f"CrossSection radius must be > 0, got {self.radius}")
        if not (0.0 <= self.euler_p <= 1.0):
            raise ValueError(f"CrossSection euler_p must be in [0, 1], got {self.euler_p}")
        if self.min_straight < 0.0:
            raise ValueError(
                f"CrossSection min_straight must be >= 0, got {self.min_straight}"
            )

    def _first_by_role(self, role: str) -> LayerSpec | None:
        for spec in self.layers:
            if spec.role == role:
                return spec
        return None

    @property
    def core_layer(self) -> tuple[int, int] | None:
        spec = self._first_by_role("core")
        if spec is None and self.layers:
            spec = self.layers[0]
        return spec.layer_pair if spec is not None else None

    @property
    def core_width(self) -> float:
        spec = self._first_by_role("core")
        if spec is None and self.layers:
            spec = self.layers[0]
        return spec.width if spec is not None else 0.0

    @property
    def trench_layer(self) -> tuple[int, int] | None:
        spec = self._first_by_role("trench")
        return spec.layer_pair if spec is not None else None

    @property
    def trench_width(self) -> float:
        spec = self._first_by_role("trench")
        return spec.width if spec is not None else 0.0

    def copy(self) -> CrossSection:
        return CrossSection(
            name=self.name,
            layers=[ls.copy() for ls in self.layers],
            radius=self.radius,
            euler_p=self.euler_p,
            min_straight=self.min_straight,
        )

    def with_overrides(
        self,
        core_width: float | None = None,
        trench_width: float | None = None,
        radius: float | None = None,
        euler_p: float | None = None,
        min_straight: float | None = None,
        core_layer: tuple[int, int] | None = None,
        trench_layer: tuple[int, int] | None = None,
        extra_layers: list[LayerSpec] | None = None,
        name: str | None = None,
    ) -> CrossSection:
        """Return a new CrossSection with requested overrides applied."""
        new_layers: list[LayerSpec] = []
        has_trench = False
        has_core = False

        for ls in self.layers:
            item = ls.copy()
            if item.role == "core":
                has_core = True
                if core_width is not None:
                    item.width = float(core_width)
                if core_layer is not None:
                    item.layer, item.datatype = int(core_layer[0]), int(core_layer[1])
                if item.width > 0.0:
                    new_layers.append(item)
            elif item.role == "trench":
                has_trench = True
                if trench_width is not None:
                    if float(trench_width) <= 0.0:
                        continue
                    item.width = float(trench_width)
                if trench_layer is not None:
                    item.layer, item.datatype = int(trench_layer[0]), int(trench_layer[1])
                if item.width > 0.0:
                    new_layers.append(item)
            else:
                new_layers.append(item)

        if not has_core and core_layer is not None and core_width is not None and core_width > 0.0:
            new_layers.insert(
                0,
                LayerSpec(
                    layer=int(core_layer[0]),
                    datatype=int(core_layer[1]),
                    width=float(core_width),
                    role="core",
                ),
            )

        if not has_trench and trench_width is not None and float(trench_width) > 0.0:
            if trench_layer is not None:
                tl = (int(trench_layer[0]), int(trench_layer[1]))
            elif self.core_layer is not None:
                tl = (self.core_layer[0], self.core_layer[1] + 1)
            else:
                tl = (1, 1)
            new_layers.append(
                LayerSpec(
                    layer=tl[0],
                    datatype=tl[1],
                    width=float(trench_width),
                    role="trench",
                )
            )

        if extra_layers:
            for extra in extra_layers:
                if extra.width > 0.0:
                    new_layers.append(extra.copy())

        return CrossSection(
            name=name if name is not None else self.name,
            layers=new_layers,
            radius=self.radius if radius is None else float(radius),
            euler_p=self.euler_p if euler_p is None else float(euler_p),
            min_straight=self.min_straight if min_straight is None else float(min_straight),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "layers": [ls.to_dict() for ls in self.layers],
            "radius": self.radius,
            "euler_p": self.euler_p,
            "min_straight": self.min_straight,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CrossSection:
        return cls(
            name=str(data["name"]),
            layers=[LayerSpec.from_dict(item) for item in data.get("layers", [])],
            radius=float(data.get("radius", 50.0)),
            euler_p=float(data.get("euler_p", 0.5)),
            min_straight=float(data.get("min_straight", 5.0)),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_json(cls, text: str) -> CrossSection:
        return cls.from_dict(json.loads(text))


CROSS_SECTION_PRESETS: dict[str, CrossSection] = {
    "Core Only (Single Layer)": CrossSection(
        name="Core Only (Single Layer)",
        layers=[
            LayerSpec(layer=1, datatype=0, width=0.50, offset=0.0, role="core"),
        ],
        radius=20.0,
        euler_p=0.50,
        min_straight=5.0,
    ),
    "Core + Trench (Rib)": CrossSection(
        name="Core + Trench (Rib)",
        layers=[
            LayerSpec(layer=1, datatype=0, width=0.80, offset=0.0, role="core"),
            LayerSpec(layer=1, datatype=1, width=10.0, offset=0.0, role="trench"),
        ],
        radius=50.0,
        euler_p=0.50,
        min_straight=5.0,
    ),
    "Double Etch (2 Trenches)": CrossSection(
        name="Double Etch (2 Trenches)",
        layers=[
            LayerSpec(layer=1, datatype=0, width=0.80, offset=0.0, role="core"),
            LayerSpec(layer=1, datatype=1, width=10.0, offset=0.0, role="trench"),
            LayerSpec(layer=2, datatype=1, width=25.0, offset=0.0, role="trench"),
        ],
        radius=50.0,
        euler_p=0.50,
        min_straight=5.0,
    ),
}


def get_user_presets_path() -> Path:
    """Return the platform-appropriate JSON file path for persisted user presets."""
    try:
        import pya  # type: ignore[import-not-found]

        app = pya.Application.instance() if hasattr(pya, "Application") else None
        if app is not None and hasattr(app, "application_data_path"):
            app_dir = app.application_data_path()
            if app_dir:
                return Path(str(app_dir)) / "pic_router_presets.json"
    except Exception:
        pass

    base_dir = Path.home() / ("KLayout" if sys.platform.startswith("win") else ".klayout")
    return base_dir / "pic_router_presets.json"


def load_user_presets(path: Path | None = None) -> dict[str, CrossSection]:
    """Load user-saved cross-section presets from JSON (returns {} if absent/unreadable)."""
    p = Path(path) if path is not None else get_user_presets_path()
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}

    entries = raw.get("presets", raw) if isinstance(raw.get("presets"), dict) else raw
    result: dict[str, CrossSection] = {}
    for raw_name, item in entries.items():
        name = str(raw_name).strip()
        if not name or name == "Custom" or name in CROSS_SECTION_PRESETS:
            continue
        if not isinstance(item, dict):
            continue
        try:
            payload = dict(item)
            payload["name"] = name
            result[name] = CrossSection.from_dict(payload)
        except Exception:
            continue
    return result


def _write_user_presets_file(p: Path, presets: dict[str, CrossSection]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "presets": {k: presets[k].to_dict() for k in sorted(presets.keys())},
    }
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def save_user_preset(name: str, cs: CrossSection, path: Path | None = None) -> Path:
    """Persist a custom user cross-section preset to JSON and return the file path."""
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Preset name cannot be empty.")
    if clean_name == "Custom" or clean_name in CROSS_SECTION_PRESETS:
        raise ValueError(
            f"Preset name {clean_name!r} is reserved for a built-in template; choose another name."
        )

    p = Path(path) if path is not None else get_user_presets_path()
    presets = load_user_presets(p)
    cs_copy = cs.copy()
    cs_copy.name = clean_name
    presets[clean_name] = cs_copy
    _write_user_presets_file(p, presets)
    return p


def delete_user_preset(name: str, path: Path | None = None) -> bool:
    """Delete a user-saved preset from JSON. Built-in templates cannot be deleted."""
    clean_name = str(name or "").strip()
    if not clean_name or clean_name == "Custom" or clean_name in CROSS_SECTION_PRESETS:
        return False

    p = Path(path) if path is not None else get_user_presets_path()
    presets = load_user_presets(p)
    if clean_name not in presets:
        return False
    del presets[clean_name]
    _write_user_presets_file(p, presets)
    return True


def get_all_presets(path: Path | None = None) -> dict[str, CrossSection]:
    """Return built-in starter templates merged with user-saved JSON presets."""
    merged: dict[str, CrossSection] = {
        k: v.copy() for k, v in CROSS_SECTION_PRESETS.items()
    }
    user_presets = load_user_presets(path)
    for name, cs in user_presets.items():
        if name not in CROSS_SECTION_PRESETS and name != "Custom":
            merged[name] = cs.copy()
    return merged


def get_cross_section(name: str, path: Path | None = None) -> CrossSection:
    """Return a fresh copy of a named built-in or user-saved cross-section preset."""
    if name in CROSS_SECTION_PRESETS:
        return CROSS_SECTION_PRESETS[name].copy()
    user_presets = load_user_presets(path)
    if name in user_presets:
        return user_presets[name].copy()
    valid = ", ".join(sorted(list(CROSS_SECTION_PRESETS.keys()) + list(user_presets.keys())))
    raise KeyError(f"Unknown cross-section preset {name!r}; available: {valid}")
