"""ZIP code to point lookup (offline, via pgeocode's GeoNames data)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache

INVALID_ZIP_MESSAGE = "Enter a valid 5-digit US ZIP"
_ZIP_RE = re.compile(r"^\d{5}$")


class InvalidZipError(ValueError):
    def __init__(self, zip_code: object):
        super().__init__(INVALID_ZIP_MESSAGE)
        self.zip_code = zip_code


@dataclass(frozen=True)
class Location:
    zip: str
    lat: float
    lon: float
    place: str


@lru_cache(maxsize=1)
def _nominatim():
    import pgeocode

    return pgeocode.Nominatim("us")


def resolve_zip(zip_code: str) -> Location:
    """Resolve a US ZIP to a Location. Raises InvalidZipError for bad or unknown ZIPs."""
    z = str(zip_code or "").strip()
    if not _ZIP_RE.match(z):
        raise InvalidZipError(zip_code)

    row = _nominatim().query_postal_code(z)
    lat, lon = row.latitude, row.longitude
    if lat is None or lon is None or _isnan(lat) or _isnan(lon):
        raise InvalidZipError(zip_code)

    place = row.place_name if isinstance(row.place_name, str) else ""
    state = row.state_code if isinstance(row.state_code, str) else ""
    label = ", ".join(p for p in (place, state) if p) or z
    return Location(zip=z, lat=float(lat), lon=float(lon), place=label)


def _isnan(x) -> bool:
    try:
        return math.isnan(float(x))
    except (TypeError, ValueError):
        return True
