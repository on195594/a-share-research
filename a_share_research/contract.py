"""判断契约：surface 是决定模型被问了什么、程序如何冻结被记分字段的全部文本与常量。

任何使 surface 哈希变化的修改必须在本文件选择并记录：语义变化递增 JUDGMENT_CONTRACT
并更新哈希；纯重排、注释等非语义变化只更新哈希、版本不变，并说明原因。
契约面清单由 tests/test_judgment_contract.py 机械钉住；修改遵守判断验证文档的季度窗口。
"""

JUDGMENT_CONTRACT = 1
SURFACE_SHA256 = "c810df729b32b40ae8dfd0a3ce6f58eb692d777c2dcc3f5ee84a7dede692fd96"
CHANGES = [(1, "2026-11-10", "当前提示词、工具定义与冻结规则的基线，未改变研究语义。")]
