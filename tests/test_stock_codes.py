"""Stock-code parsing examples and explicitly invalid format variants."""

import pytest

from a_share_research.stock_codes import infer_ts_code, normalize_key, parse_code


@pytest.mark.parametrize("key,suffix", [
    ("603606", "SH"),
    ("000333", "SZ"),
    ("831001", "BJ"),
    ("300750", "SZ"),
])
@pytest.mark.parametrize("explicit_suffix", [False, True])
@pytest.mark.parametrize("padding", ["", " \t\n"])
def test_valid_codes(key, suffix, explicit_suffix, padding):
    code = f"{key}.{suffix}" if explicit_suffix else key
    code = f"{padding}{code}{padding}"
    assert parse_code(code) == (key, suffix if explicit_suffix else None)
    assert normalize_key(code) == key
    assert infer_ts_code(code) == f"{key}.{suffix}"


@pytest.mark.parametrize("code", [
    "603606.SS",
    "abc",
    "60360",
    "6036060",
    "\u0660\u0661\u0662\u0663\u0664\u0665",
    "６０３６０６.SH",
    "603606.sh",
    "603606. SH",
    "603 606",
    "603606\n.SH",
    "603606.SH.extra",
    None,
    "",
    " \t\n",
    603606,
    b"603606",
])
@pytest.mark.parametrize("parse", [parse_code, normalize_key, infer_ts_code])
def test_invalid_codes(code, parse):
    with pytest.raises(ValueError) as error:
        parse(code)
    assert str(error.value) == "股票代码必须为6位数字或带.SH/.SZ/.BJ后缀"


def test_explicit_suffix_is_preserved():
    # Deliberately mismatched exchange: explicit suffixes are not inferred again.
    assert infer_ts_code("603606.SZ") == "603606.SZ"
