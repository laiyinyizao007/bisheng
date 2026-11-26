# copilot 评测：baseline-web-reader

- 时间：20261125-050027
- run_id：e128ff
- label：baseline-web-reader
- cases：1
- repeat：3
- runs：3
- default_model_id：7
- judge_model：MiniMax-M3
- platform：{'code_node_enabled': True}
- skipped：[]
- judge_calibrated：True
- model_calls：9
- elapsed_s：32

## 汇总

| 组 | 指标 | 结果 |
|---|---|---|
| clear | 方案通过结构校验 | 100%（3/3，95% 区间 44–100%） |
| clear | 硬性要求满足 | 100%（3/3，95% 区间 44–100%） |
| clear | 不该问时问了（误问率） | 0%（0/3，95% 区间 0–56%） |
| clear | 样例走查正确 | 100%（3/3，95% 区间 44–100%） |
| all | 方案通过结构校验 | 100%（3/3，95% 区间 44–100%） |
| all | 硬性要求满足 | 100%（3/3，95% 区间 44–100%） |
| all | 样例走查正确 | 100%（3/3，95% 区间 44–100%） |

## 逐条

| 用例 | 组 | 次 | 问了 | 结构 | 硬性 | 走查 | 做不到 | 错误 |
|---|---|---|---|---|---|---|---|---|
| clear-web-reader | clear | 0 | ✗ | ✓ | ✓ | PASS | — |  |
| clear-web-reader | clear | 1 | ✗ | ✓ | ✓ | PASS | — |  |
| clear-web-reader | clear | 2 | ✗ | ✓ | ✓ | PASS | — |  |
