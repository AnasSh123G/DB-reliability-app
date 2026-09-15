"""Project-wide configuration constants."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Populate os.environ from a plain KEY=value .env file.

    setdefault, not assignment: a real environment variable must beat a stale
    checked-out file, because the deployment path is cron. Deliberately not
    python-dotenv — the format is trivial and the project has few dependencies.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _first_env(*names: str) -> tuple[str | None, str]:
    """Return (value, which_name_it_came_from) for the first name that is set."""
    for name in names:
        if os.environ.get(name):
            return os.environ[name], name
    return None, "<unset>"


_load_dotenv()

DATA_DIR = Path(os.environ.get("DBREL_DATA_DIR", "data"))
CACHE_DIR = DATA_DIR / "feeds"
DB_PATH = Path(os.environ.get("DBREL_DB", DATA_DIR / "dbrel.sqlite"))

TZ = ZoneInfo("Europe/Berlin")

STATIC_FEEDS = {
    "fv": "https://download.gtfs.de/germany/fv_free/latest.zip",
    "rv": "https://download.gtfs.de/germany/rv_free/latest.zip",
}
REALTIME_URL = "https://realtime.gtfs.de/realtime-free.pb"

# gtfs.de serves the realtime feed uncompressed (~47 MB per fetch, no gzip offered),
# so poll cadence is a real bandwidth cost. See docs/probe-findings.md.
HTTP_TIMEOUT_S = 300.0

# Rail only. 2 is the classic GTFS rail type; 100-117 are the extended rail types.
# The gtfs.de fv/rv feeds only use 2, but accept the extended range defensively.
RAIL_ROUTE_TYPES = frozenset({2, *range(100, 118)})

# --- DB Timetables API (IRIS-TTS) ------------------------------------------
# Both headers are required; either alone returns 401. See docs/probe-findings.md §9.
TT_BASE_URL = os.environ.get(
    "DBREL_TT_BASE_URL",
    "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1",
)
TT_API_KEY, TT_API_KEY_SOURCE = _first_env("DBREL_TT_API_KEY", "TIMETABLES_API_KEY", "DB_API_KEY")
TT_CLIENT_ID, TT_CLIENT_ID_SOURCE = _first_env("DBREL_TT_CLIENT_ID", "DB_CLIENT_ID")

# The gateway advertises name=default,60 in x-ratelimit-limit. Work to 50 and keep
# the remainder for retries; the bucket is shared across processes via a lock file.
TT_RPM = int(os.environ.get("DBREL_TT_RPM", "50"))
TT_RATELIMIT_STATE = DATA_DIR / "tt_ratelimit.json"
TT_SWEEP_LOCK = DATA_DIR / "tt_sweep.lock"

# Small XML documents, not the 47 MB GTFS-RT blob.
TT_HTTP_TIMEOUT_S = 30.0

# /plan serves only today and tomorrow, and even tomorrow fills in gradually
# (findings §15), so the importer rolls forward rather than batching a day.
TT_PLAN_LEAD_HOURS = int(os.environ.get("DBREL_TT_PLAN_LEAD_HOURS", "3"))

# German railway stations occupy this eva range. Six-digit evas are DELFI bus and
# tram halts, which /station/ will happily return for a name query (findings §14).
RAIL_EVA_RANGE = range(8_000_000, 8_100_000)

# --- Aggregates ------------------------------------------------------------
# DB's own threshold: a train under six minutes late counts as pünktlich. Using
# the operator's definition means the published figures are comparable.
PUNCTUAL_S = 360

AVERAGES_RULE_VERSION = 1

# Below this many samples the UI shows "—" rather than a number. An average over
# two runs is noise, and printing it is worse than printing nothing.
AVG_MIN_SAMPLES = 5

# --- Station board ---------------------------------------------------------
# A service day runs past midnight; GTFS seconds-of-day exceed 86400 accordingly.
SERVICE_DAY_MAX_S = 30 * 3600
BOARD_BACK_S = 15 * 60
BOARD_FWD_S = 90 * 60
BOARD_LIMIT = 200

# --- Actual-time derivation (rule_version 1) -------------------------------
RULE_VERSION = 1

# An event is considered to have happened this long after its predicted time.
GRACE_S = 120

# A value must hold still this long past passage to be graded OBSERVED.
SETTLE_S = 300

# A service date is closed this long after its last scheduled arrival. Also how
# long finalize waits before stamping a run finalized_at: past that point no
# further read can add anything, so the grades are safe to freeze.
SERVICE_DATE_CLOSE_S = 6 * 3600

# How long a passed event stays visible in the /fchg horizon. §18 could only show
# it exceeds 75 minutes, because the pilot's watch ended before the API dropped
# anything. Measured properly on 2026-09-12 over 21 stations: values were still
# being returned 15 hours after the event, tapering smoothly (86% of 717 recorded
# values within 8 hours), so retention is an order of magnitude longer than the
# floor suggested.
#
# It decides whether a missing value means "the railway said nothing" (NO_DATA) or
# "we were not reading" (PREDICTION_ONLY). Set to 6 hours: well inside the observed
# range, and equal to SERVICE_DATE_CLOSE_S so the window we check for poll coverage
# is exactly the period over which the grades are still allowed to change.
TT_LINGER_S = int(os.environ.get("DBREL_TT_LINGER_S", 6 * 3600))
