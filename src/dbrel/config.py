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
TT_API_KEY, TT_API_KEY_SOURCE = _first_env(
    "DBREL_TT_API_KEY", "TIMETABLES_API_KEY", "TIMETABLES_API_KEY_1", "DB_API_KEY"
)
TT_CLIENT_ID, TT_CLIENT_ID_SOURCE = _first_env(
    "DBREL_TT_CLIENT_ID", "DB_CLIENT_ID", "DB_CLIENT_ID_1"
)

# --- Request rate: this is the ONLY place these numbers are set --------------
# Nothing else in the app, its docs, or its scripts should assert a specific rate;
# they should point here instead. (docs/probe-findings.md §27 has the measurement
# this is based on; README's "Request rate" section explains it for a reader.)
#
# The gateway advertises name=default,60 in x-ratelimit-limit, but measurement says it
# does not enforce it: 1,500 sequential /station calls at ~44/s and 300 /fchg calls at
# ~2,560/min both drew zero 429s, with x-ratelimit-remaining pinned at 0 throughout.
#
# So the limiter runs fast by default and degrades on evidence rather than on the
# advertised number: any 429 drops every process to the advertised-legal rate for
# TT_PENALTY_S, after which it returns to full speed on its own. If DB ever switches
# enforcement on, that is a three-minute slowdown instead of a wall of failures.
TT_RPS_FAST = int(os.environ.get("DBREL_TT_RPS_FAST", "60"))
TT_RPM_PENALTY = int(os.environ.get("DBREL_TT_RPM_PENALTY", "58"))
TT_PENALTY_S = int(os.environ.get("DBREL_TT_PENALTY_S", "180"))

# How many /plan buckets may be in flight at once. One connection tops out near 45/s on
# this API's ~22 ms round trip, below TT_RPS_FAST, so the backfill needs a little
# concurrency to reach the rate the limiter allows. Fetch only — parsing and every write
# stay on the calling thread, because a SQLAlchemy Session is not thread-safe.
TT_FETCH_WORKERS = int(os.environ.get("DBREL_TT_FETCH_WORKERS", "8"))
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

# Below this many samples the UI marks the average "provisional" rather than
# hiding it — a figure from one run is still the truest number available while
# a fresh, no-synthetic-data collection is under way, it is just noisier than
# five runs. Below AVG_MIN_SAMPLES, printed but flagged; at or above, plain.
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

# --- Continuous collection (dbrel collect) ---------------------------------
# Checked into the repo (data/ is gitignored) — see docs/probe-findings.md §22 for
# why re-resolving these by name after a reset is risky rather than free.
DEFAULT_STATIONS_FILE = Path(os.environ.get("DBREL_STATIONS_FILE", "config/ruhr_stations.toml"))

# One /fchg sweep is one call per tracked station — about 4 seconds for 212 of them
# at TT_RPS_FAST — so the cadence is chosen for how promptly a passed event is
# recorded, not for cost. 15 minutes is well inside TT_LINGER_S (6h, past which an
# unread stop is graded "we weren't listening" rather than "nothing happened") and
# inside /fchg's ~15h retention (§20). Going below ~2 minutes buys nothing: GRACE_S
# means an event is not recordable until 120 s after it passes.
COLLECT_INTERVAL_S = int(os.environ.get("DBREL_COLLECT_EVERY_S", 15 * 60))
COLLECT_DURATION_S = int(os.environ.get("DBREL_COLLECT_FOR_S", 7 * 86400))

# finalize and compute-averages are CPU over the whole history, not API calls, and
# nothing they produce changes faster than the poll that feeds them. Running them on
# every 15-minute cycle would repeat thousands of rows of work 96 times a day, so they
# run every Nth cycle instead; backfill and poll still run every cycle.
COLLECT_DERIVE_EVERY = int(os.environ.get("DBREL_COLLECT_DERIVE_EVERY", "4"))

# How far back a cycle's /plan window may reach even if the previous cycle ran
# long ago. /plan purges past hours of today after ~14h (§24), and a 404'd
# bucket is NOT skipped by the fetch dedup (only a 200 is), so a longer window
# costs one live, wasted API call per station per hour, every single cycle.
COLLECT_MAX_LOOKBACK_S = int(os.environ.get("DBREL_COLLECT_MAX_LOOKBACK_S", 16 * 3600))

# The 1-hour safety margin added on top of "time since the last cycle", so a
# cycle that ran slightly late does not leave a bucket uncovered.
COLLECT_OVERLAP_S = 3600

# Consecutive failed cycles before a long unattended run gives up rather than
# quietly doing nothing for days (e.g. a revoked credential, not a blip).
COLLECT_MAX_FAILED_CYCLES = 5

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
