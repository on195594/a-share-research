"""Replay the stored inputs of production policy_evaluations against their recorded outputs.

The samples hold the owner's real holdings, so they live in the private evidence
directory, never in this public repository.
"""
import gzip
import json
import os
from pathlib import Path

import pytest

from a_share_research.policy import daily_evaluation, evaluation, holdings_check_evaluation, stop_evaluation


EVALUATE = {
    'policy.daily_brief': daily_evaluation.evaluate,
    'policy.holdings_check': holdings_check_evaluation.evaluate,
    'policy.monitor': evaluation.evaluate,
    'policy.stop_alert': stop_evaluation.evaluate,
}
SOURCE = Path(os.environ.get('POLICY_SAMPLES', Path.home() / '.local/share/a-share-research-evaluations/policy-samples/policy-evaluations-20261120.json.gz'))
if not SOURCE.exists():
    pytest.skip(f'private policy samples not found: {SOURCE}', allow_module_level=True)
with gzip.open(SOURCE, 'rt', encoding='utf-8') as stream:
    SAMPLES = json.load(stream)


@pytest.mark.parametrize('sample', SAMPLES, ids=lambda row: f"{row['action']}-{row['id']}")
def test_policy_sample(sample):
    bundle = sample['input']
    assert EVALUATE[sample['action']](bundle, bundle['risk_policy']) == sample['result']
