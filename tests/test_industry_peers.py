"""SW2021 peer lookup boundary, using a real index_member_all capture for 东方电缆 (2026-10-29)."""

import json
from pathlib import Path

import pandas as pd
import pytest

from a_share_research.sources import SourceError, peer_members

CAPTURE = json.loads((Path(__file__).parent / "data" / "sw-industry-members-603606.json").read_text())
SUBJECT, MEMBERS = pd.DataFrame(CAPTURE["subject"]), pd.DataFrame(CAPTURE["l3_members"])


def test_peers_exclude_subject_and_keep_classification_provenance():
    result = peer_members(SUBJECT, MEMBERS, "l3")
    codes = [m["code"] for m in result["members"]]
    assert "603606" not in codes and len(codes) == len(MEMBERS) - 1
    assert {"600973", "002498"} <= set(codes)  # 宝胜股份、汉缆股份
    assert result["industry"]["l3"] == "线缆部件及其他" and result["classification"] == "SW2021"
    assert codes == sorted(codes)


def test_mismatched_or_ambiguous_membership_is_rejected():
    with pytest.raises(SourceError, match="其他行业"):
        peer_members(SUBJECT, MEMBERS.assign(l3_code="000000.SI"), "l3")
    with pytest.raises(SourceError, match="记录数"):
        peer_members(pd.concat([SUBJECT, SUBJECT]), MEMBERS, "l3")
    with pytest.raises(SourceError, match="缺少"):
        peer_members(SUBJECT.drop(columns=["l3_code"]), MEMBERS, "l3")
