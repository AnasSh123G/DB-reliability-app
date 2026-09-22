"""Parser tests. Pure: no network, no database.

Each case pins down something the probe had to measure, so a regression here
means the ingest has quietly stopped agreeing with the API.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from dbrel.config import TZ
from dbrel.identity import (
    line_aliases,
    parse_query,
    path_sha1,
    prefix_range,
    search_norm,
    segment_of,
    train_aliases,
    train_identity_key,
)
from dbrel.ingest.parser import (
    parse_timetable,
    parse_ts,
    run_key,
    service_date_of,
    split_stop_id,
)
from dbrel.ingest.stations import loose_name, query_variants

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

    def test_positive_daily_trip_id(self):
        assert split_stop_id("7825829770140099707-2609110452-8") == (
            "7825829770140099707",
            "2609110452",
            8,
        )

    def test_run_key_is_identical_at_every_station(self):
        """The property that stitches one physical run into one trip row."""
        at_bochum = "-5962154382149198734-2609110526-6"
        at_dortmund = "-5962154382149198734-2609110526-11"
        assert run_key(at_bochum) == run_key(at_dortmund) == "-5962154382149198734-2609110526"

    def test_service_date_is_the_origin_departure_date(self):
        assert service_date_of("2609110526") == "2026-09-11"

    def test_malformed_ids_are_rejected_not_guessed(self):
        assert split_stop_id("nonsense") is None
        assert split_stop_id("123-456-7") is None  # middle field must be 10 digits


class TestTimeParsing:
    def test_ten_digit_local_wall_clock(self):
        assert parse_ts("2609110646") == epoch(2026, 9, 11, 6, 46)

    def test_rejects_the_six_digit_form_the_swagger_prose_claims(self):
        assert parse_ts("260911") is None

    def test_none_and_junk(self):
        assert parse_ts(None) is None
        assert parse_ts("") is None
        assert parse_ts("26091106xx") is None

    def test_crosses_a_dst_boundary(self):
        """2026-10-25 is the autumn changeover; both 02:30s exist."""
        before = parse_ts("2610250130")
        after = parse_ts("2610250330")
        assert after - before == 3 * 3600  # 2 wall-clock hours, 3 real hours


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

    def test_stop_index_is_one_based_relative_to_ppth(self):
        """The invariant measured over 1,588 stops: len(ar@ppth) == stopIndex - 1."""
        parsed = parse_timetable(PLAN_XML)
        for stop in parsed.stops:
            assert len(stop.arrival.path_names()) == stop.stop_index - 1

    def test_long_distance_has_no_line_regional_does(self):
        parsed = parse_timetable(PLAN_XML)
        ice, rb = parsed.stops
        assert ice.line is None and ice.is_long_distance
        assert rb.line == "43" and not rb.is_long_distance

    def test_reads_the_undocumented_fb_label(self):
        assert parse_timetable(PLAN_XML).stops[0].label == "ICE 843"

    def test_wings_are_run_keys_not_stop_ids(self):
        wings = parse_timetable(PLAN_XML).stops[0].arrival.wings
        assert wings == "4287727884008610000-2609110526"
        assert split_stop_id(wings + "-1") is not None  # joins to a trip

    def test_keep_filters_before_building_objects(self):
        parsed = parse_timetable(PLAN_XML, keep=lambda key: key.startswith("-5962"))
        assert len(parsed.stops) == 1
        assert parsed.n_raw_stops == 2  # both were seen, one was kept

    def test_delay_is_computed_since_iris_sends_none(self):
        stop = parse_timetable(FCHG_XML).stops[0]
        assert stop.arrival.delay_s == 7 * 60
        assert stop.departure.delay_s == 7 * 60

    def test_one_sided_cancellation_is_representable(self):
        """Departure cancelled, arrival not — a case an int enum could not carry."""
        stop = parse_timetable(FCHG_XML).stops[0]
        assert not stop.arrival.is_cancelled
        assert stop.departure.is_cancelled
        assert stop.departure.cancelled_at == epoch(2026, 9, 10, 21, 5)

    def test_messages_carry_the_delay_cause(self):
        message = parse_timetable(FCHG_XML).stops[0].messages[0]
        assert (message.code, message.category, message.priority) == (80, "Verzoegerung", 2)

    def test_change_detection_tuple_moves_only_on_a_real_change(self):
        first = parse_timetable(FCHG_XML).stops[0]
        again = parse_timetable(FCHG_XML).stops[0]
        assert first.value() == again.value()
        again.arrival.changed_ts += 60
        assert first.value() != again.value()


class TestIdentity:
    def test_number_is_identity_and_path_is_not(self):
        """Growing the tracked set must not fork a train's history."""
        eight_stations = train_identity_key("tts", "ICE", "843", path_hash=path_sha1(["A", "B"]))
        three_hundred = train_identity_key("tts", "ICE", "843", path_hash=path_sha1(["A", "X", "B"]))
        assert eight_stations == three_hundred == "tts:ICE/843"

    def test_sources_can_never_collide(self):
        assert train_identity_key("gtfs", None, None, legacy_key="EC/1/2/abc").startswith("gtfs:")
        assert train_identity_key("tts", "ICE", "843").startswith("tts:")

    def test_pattern_fallback_when_the_number_is_missing(self):
        key = train_identity_key("tts", "RB", None, path_hash="a" * 40, start_hhmm="0526")
        assert key == "tts:pattern/RB/0526/aaaaaaaaaaaaaaaa"

    def test_path_hash_uses_supplied_names_not_observed_stops(self):
        assert path_sha1(["Köln Hbf", "Essen Hbf"]) == path_sha1(["koln hbf", "ESSEN HBF"])

    @pytest.mark.parametrize(
        "text,expected", [("ICE 599", "ice599"), ("ice-599", "ice599"), ("ICE599", "ice599")]
    )
    def test_search_normalisation_collapses_spacing(self, text, expected):
        assert search_norm(text) == expected

    def test_query_parsing(self):
        assert parse_query("ICE 599") == ("ice", "599")
        assert parse_query("S1") == ("s", "1")
        assert parse_query("4523") == ("", "4523")
        assert parse_query("Köln Hbf") is None

    def test_a_train_never_answers_to_a_line_name(self):
        """The collapse Rule 2 exists to prevent: 'S1' must not match 249 trains."""
        aliases = {a for a, _, _, _ in train_aliases("S", "31415")}
        assert aliases == {"s31415", "31415"}
        assert "s1" not in aliases

    def test_a_numeric_line_does_not_squat_on_a_bare_number(self):
        aliases = {a for a, _, _, _ in line_aliases("S", "4")}
        assert aliases == {"s4"}  # not '4', which belongs to train number 4

    def test_line_label_is_not_stuttered_with_the_operator_brand(self):
        """dp@l already reads 'RE4'; 'NX RE4' would be the operator plus a label."""
        from dbrel.identity import line_display_name

        assert line_display_name("NX", "RE4") == "RE4"
        assert line_display_name("RB", "RB32") == "RB32"
        assert line_display_name("S", "S1") == "S1"
        assert line_display_name("S", "1") == "S1"  # bare number still gets a prefix
        primary = line_aliases("NX", "RE4")[0]
        assert primary[0] == "re4" and primary[1] == "RE4"

    def test_prefix_range_is_half_open(self):
        lo, hi = prefix_range("ice5")
        assert lo == "ice5" and hi == "ice6"
        assert lo <= "ice599" < hi
        assert not (lo <= "ice6" < hi)

    def test_segment_split_follows_the_line_boundary(self):
        assert segment_of("ICE", None) == "FV"
        assert segment_of("RE", "4") == "RV"
        assert segment_of("S", "1") == "S"


class TestStationNameMatching:
    def test_iris_and_delfi_hyphenation_reconcile(self):
        assert loose_name("Herne-Wanne-Eickel Hbf") == loose_name("Herne Wanne-Eickel Hbf")

    def test_hbf_and_hauptbahnhof_reconcile(self):
        assert loose_name("Neuss Hbf") == loose_name("Neuss Hauptbahnhof")

    def test_different_places_stay_different(self):
        assert loose_name("Essen Hbf") != loose_name("Essen-Steele")

    def test_variants_move_a_trailing_city_to_the_front(self):
        assert "Berlin Ostkreuz" in query_variants("S Ostkreuz Bhf (Berlin)")

    def test_variants_try_the_iris_hyphenated_suburb_form(self):
        """IRIS hyphenates 'City District' where gtfs.de spaces it -- confirmed
        live against 9/10 sampled Ruhrgebiet suburb stops (§25)."""
        assert "Bochum-Dahlhausen" in query_variants("Bochum Dahlhausen")
        assert "Duisburg-Hochfeld Süd" in query_variants("Duisburg Hochfeld Süd")

    def test_variants_drop_the_trailing_s_bahn_marker_before_hyphenating(self):
        """gtfs.de appends a bare 'S' IRIS never carries in the name
        ('Essen Steele S' -> IRIS 'Essen-Steele'); confirmed live (§25)."""
        assert "Essen-Steele" in query_variants("Essen Steele S")
        assert "Essen-Steele Ost" in query_variants("Essen Steele Ost S")

    def test_variants_skip_hyphenation_when_there_is_no_space(self):
        assert query_variants("Bochum") == ["Bochum"]
