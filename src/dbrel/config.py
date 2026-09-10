"""Project-wide configuration constants."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

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

# --- Actual-time derivation (rule_version 1) -------------------------------
RULE_VERSION = 1

# An event is considered to have happened this long after its predicted time.
GRACE_S = 120

# A value must hold still this long past passage to be graded OBSERVED.
SETTLE_S = 300

# A service date is closed this long after its last scheduled arrival.
SERVICE_DATE_CLOSE_S = 6 * 3600
