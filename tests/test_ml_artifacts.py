"""Where trained models live, and which one gets loaded.

The failure this guards is quiet: a booster indexes its features positionally,
so a model trained against a different FEATURE_SPEC does not raise when loaded —
it predicts confidently from the wrong columns. Everything here is about making
that impossible to reach by accident.

Uses a stub in place of a real booster, so it runs without the `ml` extra.
"""

from __future__ import annotations

from dbrel.ml._common import FEATURE_NAMES, QUANTILES
from dbrel.ml.artifacts import Manifest, latest, load_manifest, new_model_id, save


class StubBooster:
    """Stands in for lgb.Booster: the only thing artifacts.save needs is this."""

    def __init__(self, tag="x"):
        self.tag = tag

    def save_model(self, path):
        with open(path, "w") as handle:
            handle.write(self.tag)


def manifest(model_id, *, active=True, features=None, adjustment=12.0):
    return Manifest(
        model_version=1,
        model_id=model_id,
        trained_at=1,
        active=active,
        feature_names=list(features if features is not None else FEATURE_NAMES),
        quantiles=list(QUANTILES),
        conformal_adjustment=adjustment,
        vocabularies={"train_id": {"500": 0}},
    )


def boosters():
    return {q: StubBooster(f"q{q}") for q in QUANTILES}


def test_a_saved_model_round_trips(tmp_path):
    directory = save(boosters(), manifest("20260101T000000Z"), root=tmp_path)
    loaded = load_manifest(directory)
    assert loaded.model_id == "20260101T000000Z"
    assert loaded.conformal_adjustment == 12.0
    assert loaded.vocabularies == {"train_id": {"500": 0}}
    assert loaded.feature_names == list(FEATURE_NAMES)


def test_every_quantile_gets_a_file(tmp_path):
    directory = save(boosters(), manifest("20260101T000000Z"), root=tmp_path)
    for quantile in QUANTILES:
        assert (directory / f"q{int(quantile * 100)}.txt").is_file()


def test_latest_returns_the_newest_activated_run(tmp_path):
    save(boosters(), manifest("20260101T000000Z"), root=tmp_path)
    save(boosters(), manifest("20260301T000000Z"), root=tmp_path)
    save(boosters(), manifest("20260201T000000Z"), root=tmp_path)
    found = latest(root=tmp_path)
    assert found is not None
    assert found[1].model_id == "20260301T000000Z"


def test_a_rejected_run_is_unreachable(tmp_path):
    """A model that lost to the baseline must not be loadable at all.

    Not merely deprioritised — if it were the only model present, `latest` has to
    return nothing rather than fall back to the one that was judged worse than
    the average it would replace.
    """
    directory = save(boosters(), manifest("20260101T000000Z", active=False), root=tmp_path)
    assert "_rejected" in str(directory)
    assert latest(root=tmp_path) is None


def test_a_model_from_a_different_feature_spec_is_invisible(tmp_path):
    """The positional-indexing trap. Stale features must not merely rank last."""
    save(boosters(), manifest("20260101T000000Z", features=["only", "two"]), root=tmp_path)
    assert latest(root=tmp_path) is None


def test_an_empty_store_returns_nothing(tmp_path):
    assert latest(root=tmp_path / "nothing-here") is None


def test_model_ids_sort_chronologically():
    """`latest` picks by name, so the id format has to sort the right way."""
    early = new_model_id(now=1_700_000_000)
    late = new_model_id(now=1_800_000_000)
    assert early < late
