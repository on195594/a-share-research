## 阈值历史回放

`scripts/policy_replay.py` 用 point-in-time 日度快照比较当前 Tier/止损/估值退出与买入持有。
它只提供可复现的验证框架，不声明默认阈值有效。

输入 CSV：

```csv
date,close,valuation_percentile,dividend_per_share
2024-01-02,10.00,0.35,0
2024-01-03,10.20,0.37,0
```

- `valuation_percentile` 必须是当日可知的历史分位，范围0–1，不能用今天回看重算出的
  全样本分位；
- 信号在T日收盘观察，统一在T+1收盘执行，避免同bar前视；
- `dividend_per_share` 在实际除息/到账节点记录；
- `--fee-bps` 纳入卖出摩擦成本；实际复盘还应使用真实佣金和印花税。

运行（`<skill_dir>` 为本技能目录）：

```bash
python3 "<skill_dir>/scripts/policy_replay.py" <快照.csv> --fee-bps 10
python3 "<skill_dir>/scripts/policy_replay.py" <快照.csv> --fee-bps 10 --grid
```

每个框架至少覆盖一个完整牛熊/景气周期，并报告总回报、CAGR、最大回撤、换手率和
交易次数。样本不足、历史分位有幸存者偏差或缺少 point-in-time 估值时，只能标注
“未验证”，不得据此调参。参数选择需留出样本外区间，不能用同一全样本选择并宣称
有效。
