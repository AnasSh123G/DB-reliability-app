"""Parser tests. Pure: no network, no database.

Each case pins down something the probe had to measure, so a regression here
means the ingest has quietly stopped agreeing with the API.
"""

from __future__ import annotations

from datetime import datetime


from dbrel.config import TZ
from dbrel.ingest.parser import (
    parse_timetable,
    parse_ts,
    split_stop_id,
)

# A real /plan response, trimmed. The wings example is verbatim from the probe.
PLAN_XML = b"""<?xml version="1.0"?>
<timetable station="Gelsenkirchen Hbf" eva="8000118">
  <s id="-5962154382149198734-2609110526-6">
    <tl f="F" t="p" o="80" c="ICE" n="843"/>
    <ar pt="2609110646" pp="16" fb="ICE 843" wings="4287727884008610000-2609110526"
        ppth="Koeln Hbf|Duesseldorf Hbf|Duesseldorf Flughafen|Duisburg Hbf|Essen Hbf"/>
    <dp pt="2609110647" pp="16" fb="ICE 843"
        ppth="Wanne-Eickel Hbf|Dortmund Hbf"/>
  </s>
  <s id="7825829770140099707-2609110452-2">
    <tl t="p" o="R7" c="RB" n="20315"/>
    <ar pt="2609110515" pp="3" ppth="Essen Hbf"/>
    <dp pt="2609110516" pp="3" l="43" ppth="Wanne-Eickel Hbf"/>
  </s>
</timetable>"""

FCHG_XML = b"""<?xml version="1.0"?>
<timetable station="Bochum Hbf" eva="8000041">
  <s id="-1234567890123456789-2609102100-4">
    <tl t="p" o="800" c="RE" n="10142"/>
    <ar pt="2609102130" ct="2609102137" cp="5"/>
    <dp pt="2609102132" ct="2609102139" cs="c" clt="2609102105"/>
    <m id="r12" t="d" c="80" cat="Verzoegerung" ts="2609102110" pr="2"/>
  </s>
</timetable>"""


def epoch(y, mo, d, h, mi):
    return int(datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp())


class TestStopId:
    def test_negative_daily_trip_id_survives_rsplit(self):
        """The leading '-' belongs to the trip id, so split('-') would corrupt it."""
        assert split_stop_id("-7874571842864554321-1403311221-11") == (
            "-7874571842864554321",
            "1403311221",
            11,
        )


class TestTimeParsing:
    def test_ten_digit_local_wall_clock(self):
        assert parse_ts("2609110646") == epoch(2026, 9, 11, 6, 46)


class TestParseTimetable:
    def test_reconstructs_the_full_route_from_one_sighting(self):
        parsed = parse_timetable(PLAN_XML)
        ice = parsed.stops[0]
        full = ice.full_path("Gelsenkirchen Hbf")
        assert full == [
            "Koeln Hbf",
            "Duesseldorf Hbf",
            "Duesseldorf Flughafen",
            "Duisburg Hbf",
            "Essen Hbf",
            "Gelsenkirchen Hbf",
            "Wanne-Eickel Hbf",
            "Dortmund Hbf",
        ]


    def test_delay_is_computed_since_iris_sends_none(self):
        stop = parse_timetable(FCHG_XML).stops[0]
        assert stop.arrival.delay_s == 7 * 60
        assert stop.departure.delay_s == 7 * 60


