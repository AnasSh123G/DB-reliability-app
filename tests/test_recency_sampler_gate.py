"""Paired reports must certify identical mixing and fixed normalization."""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import summarize_recency_training as summary


def keys():
    sampler = {'kind': 'permuted-date-block-pools', 'mix_blocks': 8,
               'read_block_rows': 262144, 'pool_rows': 2097152,
               'row_coverage': 'exactly once per epoch', 'dataset_mean_weight': 1.0}
    policy = {'normalization': 'weighted batch mean / fixed full-training mean weight'}
    control = {'version': 4, 'sampler': sampler, 'recency': policy}
    weighted = copy.deepcopy(control)
    weighted['sampler']['dataset_mean_weight'] = 1.025
    return control, weighted


def test_sampler_gate_accepts_fixed_normalization_and_rejects_different_mixing():
    control, weighted = keys()
    summary.verify_sampler_pair(control, weighted)
    weighted['sampler']['mix_blocks'] = 4
    with pytest.raises(ValueError, match='sampler'):
        summary.verify_sampler_pair(control, weighted)


@pytest.mark.parametrize('change', ['missing', 'batch_normalization', 'bad_mean', 'bad_pool'])
def test_sampler_gate_refuses_uncertified_temporal_objective(change):
    control, weighted = keys()
    if change == 'missing':
        weighted.pop('sampler')
    elif change == 'batch_normalization':
        weighted['recency']['normalization'] = 'sum weighted loss / sum weights'
    elif change == 'bad_mean':
        weighted['sampler']['dataset_mean_weight'] = float('nan')
    else:
        weighted['sampler']['pool_rows'] = 1
    with pytest.raises(ValueError, match='sampler|normalization|mean'):
        summary.verify_sampler_pair(control, weighted)
