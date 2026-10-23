from a_share_research.sources import FINANCIAL_NAME


def test_financial_names_from_the_cninfo_list_are_refused_before_queueing():
    for name in ("招商银行", "中信证券", "新华保险", "中国人寿", "永安期货", "建元信托"):
        assert FINANCIAL_NAME.search(name), name
    for name in ("贵州茅台", "长江电力", "比亚迪", "东方电缆", "万华化学", "美的集团"):
        assert not FINANCIAL_NAME.search(name), name
