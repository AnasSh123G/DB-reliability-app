"""How a train is identified, and how a human finds it.

Two rules, both established by measurement (docs/probe-findings.md §10, §11):

1. **Identity is the number; the path is an attribute.** The GTFS-era key hashed
   the station sequence *as observed*, which is fatal once the source is
   station-scoped: growing the tracked set from 8 stations to 300 would change
   the hash of every train through them and fork the whole history. `(category,
   number)` had zero collisions across 1,108 pairs, so it carries identity alone.

2. **A name and a number are different entities.** "ICE 599" names a train;
   "RE 4" and "S1" name a line with many numbered trains. The split is exactly
   the FV/RV boundary: every regional stop carries dp@l, no long-distance stop
   does. Emitting "s1" as an alias of a train would return 249 rows for one query.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

SOURCE_TT = "tts"
SOURCE_GTFS = "gtfs"
# Seeded demonstration runs. A trip.source value, not a boolean on the row: the
# column already discriminates sources, and one more string keeps every query
# that filters by provenance working unchanged.
SOURCE_SYNTH = "synth"

IDENTITY_NUMBERED = "NUMBERED"
IDENTITY_PATTERN = "PATTERN"

SEGMENT_FV = "FV"
SEGMENT_RV = "RV"
SEGMENT_S = "S"

_FV_CATEGORIES = frozenset({"ICE", "IC", "EC", "ECE", "NJ", "EN", "RJ", "RJX", "TGV", "FLX", "EUR"})
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_QUERY = re.compile(r"^(?P<cat>[a-z]{0,6})(?P<num>\d{1,6})(?P<suffix>[a-z]{0,2})$")


def normalize_name(name: str) -> str:
    """casefold + strip accents. Same contract as gtfs.static_import.normalize_name."""
    folded = unicodedata.normalize("NFKD", name.casefold())
    return "".join(c for c in folded if not unicodedata.combining(c)).strip()


def search_norm(text: str) -> str:
    """'ICE 599' / 'ice-599' / 'ICE599' all collapse to 'ice599'."""
    return _NON_ALNUM.sub("", normalize_name(text))


def parse_query(text: str) -> tuple[str, str] | None:
    """Split a search term into (category, number). None if it is free text."""
    match = _QUERY.match(search_norm(text))
    if not match:
        return None
    return match.group("cat"), match.group("num") + match.group("suffix")


def path_sha1(path_names: list[str]) -> str:
    """Hash of the source-supplied route.

    Hashes ppth — the full route as IRIS states it — never the stops we happened
    to observe, so the value does not move when the tracked station set grows.
    """
    joined = "|".join(normalize_name(n) for n in path_names)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def segment_of(category: str | None, line: str | None) -> str | None:
    """FV / RV / S — the axis the app actually slices on, replacing route_type."""
    if not category:
        return None
    upper = category.upper()
    if upper in _FV_CATEGORIES:
        return SEGMENT_FV
    if upper == "S":
        return SEGMENT_S
    return SEGMENT_RV


def train_identity_key(
    source: str,
    category: str | None,
    number: str | None,
    *,
    path_hash: str | None = None,
    start_hhmm: str | None = None,
    legacy_key: str | None = None,
) -> str:
    """The unique, source-prefixed cross-day identity of a train.

    Source-prefixed so a GTFS train and a Timetables train can never collide on
    one key while both sources run.
    """
    if source == SOURCE_TT and number:
        return f"tts:{category or '?'}/{number}"
    if source == SOURCE_TT:
        digest = (path_hash or "")[:16]
        return f"tts:pattern/{category or '?'}/{start_hhmm or '?'}/{digest}"
    return f"gtfs:{legacy_key}"


def line_identity_key(category: str | None, line: str | None) -> str:
    return f"{category or '?'}/{line or '?'}"


def train_display_name(category: str | None, number: str | None, label: str | None = None) -> str:
    """What a human sees for one numbered train. IRIS often supplies it as fb."""
    if label:
        return label
    if category and number:
        return f"{category} {number}"
    return category or number or "?"


def line_display_name(category: str | None, line: str | None) -> str:
    """The label a passenger reads: 'RE4', 'S1', 'RB32'.

    dp@l already carries the complete label in this feed — 'RE4', not '4' — even
    though the swagger's example shows a bare number. Concatenating the category
    would stutter ('NX RE4', 'RB RB32', 'S S1'), and `category` is really the
    operator's product brand (NX = National Express, ERB = Eurobahn), not a
    prefix of the line. So the line stands alone whenever it already has letters.
    """
    if line and any(c.isalpha() for c in line):
        return line
    if category and line:
        return f"{category}{line}"
    return category or line or "?"


# Alias kinds and their ranking weights. Higher wins.
ALIAS_CAT_NUMBER = ("CAT_NUMBER", 100)
ALIAS_CAT_LINE = ("CAT_LINE", 90)
ALIAS_NUMBER = ("NUMBER", 50)
ALIAS_NAME = ("NAME", 40)
ALIAS_LEGACY = ("LEGACY", 10)

ALIAS_STATION_NAME = ("STATION_NAME", 100)
ALIAS_STATION_SHORT = ("STATION_SHORT", 95)
ALIAS_STATION_TOKEN = ("STATION_TOKEN", 60)
ALIAS_STATION_DS100 = ("STATION_DS100", 50)
ALIAS_STATION_EVA = ("STATION_EVA", 40)

# Qualifiers a passenger drops when typing. "Bochum Hbf" is searched as "bochum".
_STATION_QUALIFIERS = ("hauptbahnhof", "hbf", "bahnhof", "bf")

# Suffix aliases are capped: without a limit, a long name emits a tail for every
# word and the table grows without buying any recall.
_MAX_STATION_TOKENS = 4


def train_aliases(category: str | None, number: str | None) -> list[tuple[str, str, str, int]]:
    """(alias_norm, alias_display, kind, weight) for one numbered train.

    Deliberately never emits category+line: that belongs to the line, and
    emitting it here is what would make "S1" return every S1 train ever run.
    """
    out: list[tuple[str, str, str, int]] = []
    if category and number:
        kind, weight = ALIAS_CAT_NUMBER
        out.append((search_norm(f"{category}{number}"), f"{category} {number}", kind, weight))
    if number:
        kind, weight = ALIAS_NUMBER
        out.append((search_norm(number), number, kind, weight))
    return out


def line_aliases(category: str | None, line: str | None) -> list[tuple[str, str, str, int]]:
    """Aliases for a service line.

    A bare numeric line is never emitted on its own: "4" belongs to train number
    4, and letting a line squat on it is the collapse this split exists to avoid.
    """
    if not line:
        return []
    display = line_display_name(category, line)
    kind, weight = ALIAS_CAT_LINE
    out = [(search_norm(display), display, kind, weight)]
    # Also findable as "NX RE4" when the operator brand differs from the label.
    if category and search_norm(f"{category}{line}") != search_norm(display):
        out.append((search_norm(f"{category}{line}"), f"{category} {line}", kind, weight - 10))
    return out


def station_aliases(
    name: str, ds100: str | None = None, eva: int | None = None
) -> list[tuple[str, str, str, int]]:
    """(alias_norm, alias_display, kind, weight) for one station.

    The table has carried a station_id column and a CHECK permitting it since the
    schema was written, but nothing ever wrote a row: station search had no index
    at all. This supplies it, on the same prefix-scan path as trains and lines.

    Word-boundary suffixes are the point. search_norm collapses whitespace
    entirely, so "Köln Messe/Deutz" becomes 'kolnmessedeutz' and a passenger
    typing "Deutz" matches nothing. Emitting each tail that starts on a word
    boundary — never mid-word, which would make "ochum" a hit — fixes that.
    """
    out: list[tuple[str, str, str, int]] = []
    seen: set[str] = set()

    def add(norm: str, display: str, kind_weight: tuple[str, int]) -> None:
        # The unique constraint is (alias_norm, ..., station_id), so a station may
        # not offer the same string twice. Dedupe here rather than lean on the
        # conflict clause: a suffix often equals the short form already emitted.
        if not norm or norm in seen:
            return
        seen.add(norm)
        kind, weight = kind_weight
        out.append((norm, display, kind, weight))

    add(search_norm(name), name, ALIAS_STATION_NAME)

    words = [w for w in _NON_ALNUM.split(normalize_name(name)) if w]
    if words and words[-1] in _STATION_QUALIFIERS:
        short = words[:-1]
        if short:
            add("".join(short), " ".join(short).title(), ALIAS_STATION_SHORT)
            words = short

    for index in range(1, min(len(words), _MAX_STATION_TOKENS)):
        add("".join(words[index:]), " ".join(words[index:]).title(), ALIAS_STATION_TOKEN)

    if ds100:
        add(search_norm(ds100), ds100, ALIAS_STATION_DS100)
    if eva:
        add(str(eva), str(eva), ALIAS_STATION_EVA)
    return out


def prefix_range(term: str) -> tuple[str, str]:
    """Bounds for an index-driven prefix scan.

    LIKE 'ice5%' will not use the index unless the column is NOCASE. Values are
    pre-normalised, so a half-open range does the same job and always does.
    """
    if not term:
        return "", ""
    return term, term[:-1] + chr(ord(term[-1]) + 1)
