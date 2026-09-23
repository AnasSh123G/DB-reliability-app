"""Which German Bundesland a (lat, lon) point falls in.

Point-in-polygon over the bundled GeoJSON boundary file
(`resources/de_states.geojson`), hand-rolled rather than a dependency (shapely):
the project keeps its dependency list short on purpose (see the docstring on
`holidays_de.py` for the same reasoning about the holiday ruleset), and ray
casting over 16 polygons is a few dozen lines, not a library.

The GeoJSON stores each state's German name ("Nordrhein-Westfalen"); the
holiday data (`resources/holidays_by_state.json`) keys states by ISO
3166-2:DE code ("DE-NW"). `_NAME_TO_CODE` bridges the two.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources

Ring = list[tuple[float, float]]  # [(lon, lat), ...]

_NAME_TO_CODE: dict[str, str] = {
    "Baden-Württemberg": "DE-BW",
    "Bayern": "DE-BY",
    "Berlin": "DE-BE",
    "Brandenburg": "DE-BB",
    "Bremen": "DE-HB",
    "Hamburg": "DE-HH",
    "Hessen": "DE-HE",
    "Mecklenburg-Vorpommern": "DE-MV",
    "Niedersachsen": "DE-NI",
    "Nordrhein-Westfalen": "DE-NW",
    "Rheinland-Pfalz": "DE-RP",
    "Saarland": "DE-SL",
    "Sachsen-Anhalt": "DE-ST",
    "Sachsen": "DE-SN",
    "Schleswig-Holstein": "DE-SH",
    "Thüringen": "DE-TH",
}


def _rings_of(geometry: dict) -> list[Ring]:
    """Every ring (outer boundary and holes alike) in a Polygon/MultiPolygon.

    Holes matter for point-in-polygon correctness (an enclave inside a state),
    handled by the ray-casting even/odd rule below -- a point inside a hole
    flips parity an extra time and comes out "outside" the polygon overall.
    """
    gtype = geometry["type"]
    coords = geometry["coordinates"]
    rings: list[Ring] = []
    if gtype == "Polygon":
        for ring in coords:
            rings.append([(float(x), float(y)) for x, y in ring])
    elif gtype == "MultiPolygon":
        for polygon in coords:
            for ring in polygon:
                rings.append([(float(x), float(y)) for x, y in ring])
    else:
        raise ValueError(f"unsupported geometry type: {gtype}")
    return rings


def _bbox(rings: list[Ring]) -> tuple[float, float, float, float]:
    xs = [x for ring in rings for x, _y in ring]
    ys = [y for ring in rings for _x, y in ring]
    return min(xs), min(ys), max(xs), max(ys)


def _point_in_ring(x: float, y: float, ring: Ring) -> bool:
    """Standard ray-casting (even-odd rule): cast a ray in +x, count crossings."""
    inside = False
    n = len(ring)
    x1, y1 = ring[-1]
    for x2, y2 in ring:
        if (y1 > y) != (y2 > y):
            x_at_y = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x_at_y > x:
                inside = not inside
        x1, y1 = x2, y2
    return inside


def _point_in_rings(x: float, y: float, rings: list[Ring]) -> bool:
    """Point is in the shape if it's in an odd number of rings (holes cancel out)."""
    return sum(1 for ring in rings if _point_in_ring(x, y, ring)) % 2 == 1


class _StatePolygons:
    __slots__ = ("code", "name", "rings", "bbox")

    def __init__(self, code: str, name: str, rings: list[Ring]):
        self.code = code
        self.name = name
        self.rings = rings
        self.bbox = _bbox(rings)

    def contains(self, lon: float, lat: float) -> bool:
        min_x, min_y, max_x, max_y = self.bbox
        if not (min_x <= lon <= max_x and min_y <= lat <= max_y):
            return False
        return _point_in_rings(lon, lat, self.rings)


@lru_cache(maxsize=1)
def _states() -> tuple[_StatePolygons, ...]:
    text = resources.files("dbrel.resources").joinpath("de_states.geojson").read_text()
    data = json.loads(text)
    out = []
    for feature in data["features"]:
        name = feature["properties"]["name"]
        code = _NAME_TO_CODE[name]
        rings = _rings_of(feature["geometry"])
        out.append(_StatePolygons(code, name, rings))
    return tuple(out)


def state_for(lat: float, lon: float) -> str | None:
    """The ISO 3166-2:DE code of the Bundesland containing (lat, lon), or None.

    None means the point fell outside all 16 boundaries -- a cross-border stop
    or a bad coordinate -- and is recorded as unresolved, not guessed.
    """
    for state in _states():
        if state.contains(lon, lat):
            return state.code
    return None
