"""Point-in-polygon over the bundled German state boundaries."""

from __future__ import annotations

from dbrel.geo_states import state_for


def test_known_stations_resolve_to_their_real_bundesland():
    assert state_for(48.140232, 11.558335) == "DE-BY"  # München Hbf
    assert state_for(53.552736, 10.006909) == "DE-HH"  # Hamburg Hbf
    assert state_for(50.94303, 6.958729) == "DE-NW"  # Köln Hbf
    assert state_for(51.040563, 13.732035) == "DE-SN"  # Dresden Hbf
    assert state_for(52.525592, 13.369545) == "DE-BE"  # Berlin Hbf


def test_a_point_outside_germany_resolves_to_none():
    assert state_for(48.8566, 2.3522) is None  # Paris


def test_a_point_far_off_the_planet_of_coordinates_resolves_to_none():
    assert state_for(0.0, 0.0) is None
