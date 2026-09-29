"""Discovery profile loader (RDD.md M7).

Profiles are data, not code: each ``<name>.yaml`` in this package holds the
rules ``discovery/scan.py`` applies to a scanned image - a band table for
``em_min``/``em_max`` lookup by filter, and defaults for ``calib_level`` and
``obs_collection``. Only ``generic-fits-wcs`` exists yet; ``roman-wfi`` is
M8 and is not wired in here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

_PROFILES_DIR = Path(__file__).parent


@dataclass(frozen=True)
class Band:
    em_min: float
    em_max: float


@dataclass(frozen=True)
class Profile:
    name: str
    dataproduct_type: str
    default_calib_level: int
    default_obs_collection: str
    bands: dict[str, Band] = field(default_factory=dict)


def load_profile(name: str) -> Profile:
    """Load ``<name>.yaml`` from this package's directory."""
    path = _PROFILES_DIR / f"{name}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"unknown discovery profile: {name!r} ({path} not found)")
    with path.open() as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"profile {name!r} is not a YAML mapping")

    bands_raw = data.get("bands") or {}
    bands = {
        str(key).upper(): Band(em_min=float(value["min"]), em_max=float(value["max"]))
        for key, value in bands_raw.items()
    }
    return Profile(
        name=str(data.get("name", name)),
        dataproduct_type=str(data["dataproduct_type"]),
        default_calib_level=int(data["calib_level"]),
        default_obs_collection=str(data["obs_collection"]),
        bands=bands,
    )
