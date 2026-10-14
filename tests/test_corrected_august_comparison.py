"""The matched monthly reference never uses September or fills missing runs."""
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from compare_corrected_august import DATES, trip_comparison


def test_reference_is_entire_previous_calendar_month_only():
    values=np.full((2,len(DATES)),np.nan,dtype='float32')
    values[:,:10]=0
    values[:,30]=10  # August31 must participate.
    values[:,31:]=100  # These September values must not enter the reference.
    values[:,DATES.index('2026-09-15')]=8
    scored=trip_comparison(values)
    assert scored['august_dates']==11 and scored['supported']
    np.testing.assert_allclose(scored['august_mean_min'],10/11,rtol=1e-6)
    np.testing.assert_allclose(scored['deviation_min'],8-10/11,rtol=1e-6)


def test_missing_stops_do_not_become_zero_or_gain_a_vote():
    values=np.full((5,len(DATES)),np.nan,dtype='float32')
    values[:,:10]=2
    values[:3,10]=100  # Below80%coverage: excludes this entire run.
    values[:4,11]=6  # Exactly80%: one eligible run vote.
    values[:,DATES.index('2026-09-15')]=4
    scored=trip_comparison(values)
    assert scored['august_dates']==11
    np.testing.assert_allclose(scored['august_mean_min'],26/11,rtol=1e-6)


def test_no_observed_august_or_insufficient_september_coverage_is_unmatched():
    values=np.full((2,len(DATES)),np.nan,dtype='float32')
    values[:,DATES.index('2026-09-15')]=2
    assert trip_comparison(values) is None
    values[:,:10]=1
    values[1,DATES.index('2026-09-15')]=np.nan
    assert trip_comparison(values) is None
