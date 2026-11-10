# 03 评估：测试预期、实际结果与限制

> 所有数字都来自本仓库真实执行，产物在 `examples/`，可逐条复核。

## 1. 评估环境

| 项 | 值 |
|---|---|
| 时间 | 2026-11-09 |
| 系统 / Python | macOS / 3.11.9 |
| 运行模式 | `offline_mock` 与 `llm:deepseek-flash` 各跑一轮 |
| 模型端点 | `https://api.deepseek.com`（OpenAI 兼容） |
| 测试命令 | `python3 -m unittest discover -s tests -t . -v` |

## 2. 自动化测试结果

```
Ran 40 tests in 0.030s
OK
```

原始输出见 `examples/test-results.txt`。

| 测试文件 | 用例数 | 覆盖的验收点 |
|---|---|---|
| `tests/test_tools.py` | 10 | 工具正常/异常返回、缺字段保留部分数据、规则跳过、时段占用、非法日期、冲突拒绝写、幂等、注入故障 |
| `tests/test_loop.py` | 6 | 步数上限、工具调用上限、终止动作不占工具预算、非法动作拒绝、决策层崩溃、输入非法 |
| `tests/test_idempotency.py` | 6 | 重复提交、跨实例幂等、不同时段新建、确认改期就地更新、已确认不被降级 |
| `tests/test_acceptance.py` | 10 | A1~A8 + 对照实验 C1 + 输出契约字段 |
| `tests/test_safety.py` | 7 | 无发布工具、无发布语义、硬约束护栏、失败不留草稿、密钥不入库 |

## 3. 验收矩阵实测（mock 模式）

| 编号 | 场景 | 命令 | 期望 | 实际 | 证据 |
|---|---|---|---|---|---|
| A1 | 正常排期 | `python3 main.py --input examples/a1-normal.input.json` | 直接保存 | `completed` / `pass` / `available` / `saved` / 10:00 | `a1-normal.output.json`、`trace/a1-normal.jsonl` |
| A2 | 时段冲突 | `... a2-conflict.input.json` | 建议 19:00 + 待确认 | `needs_confirmation` / `conflict_resolved` / `saved` / 19:00 / `needs_user_confirmation=true` | `a2-conflict.output.json`、`trace/a2-conflict.jsonl` |
| A3 | 信息缺失 | `... a3-info-gap.input.json` | 报缺料，不猜 | `info_gap` / `not_checked` / `not_saved`，列出 3 个缺失字段 | `a3-info-gap.output.json` |
| A4 | 规则风险 | `... a4-rule-risk.input.json` | 阻断 + 证据 | `blocked` / `risk`，R01/R02/R05 均带原文证据 | `a4-rule-risk.output.json` |
| A5 | 重复提交 | 连跑 A2 两次 | 只有一份草稿 | 第二次 `duplicate`，`draft_id` 相同，草稿总数 2（含 A1） | `a5-duplicate.output.json`、`drafts-snapshot.json` |
| A6 | 保存失败 | `... a6-save-failed.input.json` | 明确失败 | `failed` / `draft_status=failed` / `draft_id=null`，退出码 4 | `a6-save-failed.output.json` |
| A7 | 硬约束冲突 | `... a7-hard-block.input.json` | 阻断且不改期 | `blocked` / `conflict_unresolved` / `suggested_slot=null` / 无草稿 | `a7-hard-block.output.json` |
| A8 | 步数上限 | `... a8-step-limit.input.json` | 未完成但保留结论 | `incomplete`，`step_budget`，风险 3 条仍在，退出码 3 | `a8-step-limit.output.json` |
| A9 | 批量部分成功 | `... a9-batch.input.json` | 成功项不因失败项丢失 | M02 `saved`、M04 `blocked`、M01 `待确认`，13 步 / 10 次工具 | `a9-batch.output.json` |
| C1 | 受控对照 | `... c1-control-free.input.json` | 动作序列改变 | 无 `request_confirmation`，直接 18:00 保存 | `c1-control-free.output.json` |

**A2 关键片段**（这正是参考实例要求的判定）：

```json
{
  "material_id": "M01",
  "review_status": "pass",
  "requested_slot": "2026-11-10T18:00",
  "suggested_slot": "2026-11-10T19:00",
  "slot_status": "conflict_resolved",
  "needs_user_confirmation": true,
  "pending_items": ["确认把 18:00 调整为 19:00（原时段已被占用）"],
  "draft_status": "saved",
  "conclusion": "已给出建议时段，等待用户确认后才能定稿，未发布"
}
```

## 4. 受控对照实验（C1）：证明决策依赖工具返回

两次运行**只改一个字段**——`data/schedule_alt.json` 里没有 18:00 的占用记录，其余输入完全一致。

| 步骤 | a2-conflict（18:00 被占） | c1-control-free（18:00 空闲） |
|---|---|---|
| 1 | `read_material` | `read_material` |
| 2 | `check_rules` | `check_rules` |
| 3 | `query_slots` | `query_slots` |
| 4 | `save_draft(slot=19:00, needs_confirmation=true)` | `save_draft(slot=18:00, needs_confirmation=false)` |
| 5 | `request_confirmation` | `finish_material(pass)` |

同一个模型、同一份素材、同一套提示，**只有工具返回不同，动作和结论就不同**。这是"不是固定脚本"最直接的证据，评审改一个 JSON 字段即可复现。

```bash
# 复现对照：两条命令除 schedule_file 外完全等价
python3 main.py --input examples/a2-conflict.input.json      # 18:00 被占 → 建议 19:00 + 待确认
python3 main.py --input examples/c1-control-free.input.json  # 18:00 空闲 → 直接保存
```

## 5. 真实模型路径（llm 模式）

同一批输入换成 `--mode llm`（`deepseek-flash`，`temperature=0`），6 个用例的实际结果：

| 用例 | 步数/工具 | 状态 | 关键结论 |
|---|---|---|---|
| a1-normal | 5 / 4 | completed | 与 mock 完全一致 |
| a2-conflict | 5 / 4 | needs_confirmation | 主动保存 19:00 并标待确认 |
| a3-info-gap | 2 / 1 | completed(info_gap) | 读到缺字段后**直接**判定信息不足，未做无意义规则检查 |
| a4-rule-risk | 3 / 2 | blocked | 读 + 查规则后阻断，结论只引用实际违规 |
| a6-save-failed | 5 / 4 | failed | 保存失败后**没有重试**，直接上报 |
| a7-hard-block | 3 / 2 | blocked | 跳过规则检查直接查时段，确认 20:00 被占后阻断 |

产物：`examples/llm-*.output.json` 与 `examples/trace/llm-*.jsonl`。

**a6 的 trace 是"失败不重试"最直接的证据**：

```json
{"step":4,"action":"save_draft","ok":false,"error_code":"SAVE_FAILED",
 "result_summary":"注入故障：M05:save_draft","next_action":"finish_material"}
{"step":5,"action":"finish_material","args":{"outcome":"save_failed"},
 "reason":"save_draft 返回 SAVE_FAILED，按规则不得重试，如实上报草稿保存失败"}
```

**mock 与 llm 的差异本身就是结论**：a3 与 a7 两条路径步数不同（mock 先查规则再查时段，llm 直接跳到结论），说明动作序列由决策层产生，而不是由流程写死。两条路径都合法，因为最终结论一致且都有观测支撑。

## 6. 关键校验点逐条核对

| 审核要点 | 怎么被验证 | 结果 |
|---|---|---|
| 风险是否核对到规则与原文 | A4 的 `risk_reasons[].evidence` 带原文与字段值 | 通过 |
| 冲突是否调整 | A2 建议 19:00，`deviation` 留痕 | 通过 |
| 确认边界是否守住 | A2 `needs_user_confirmation=true` 且任务非 `completed`；A7 硬约束直接阻断 | 通过 |
| 保存结果是否真实 | A6 返回 `failed`、无 `draft_id`、草稿文件无增量 | 通过 |
| 是否防重复 | A5 草稿数不变、`created=false`、ID 相同 | 通过 |
| 是否越权发布 | 全量扫描输出无 `published` / "已发布"；工具表无 publish | 通过 |
| 超限是否有明确未完成 | A8 返回 `incomplete` + 原因 + 保留风险 | 通过 |
| 部分成功是否保留 | A9 批量：M02 已存草稿不受 M04 阻断影响 | 通过 |

## 7. 失败与限制（真实存在的，不是客套）

1. **单次样本不代表稳定性**。llm 模式只跑了 6 个用例各 1 次，不能证明模型在生产流量下的稳定性；`temperature=0` 也不保证跨版本一致。
2. **并发写不安全**。`runtime/drafts.json` 是整文件读写，无锁；多进程同时保存会丢更新。
3. **a3 的 llm 路径没有做规则检查**。模型判断"缺字段即无法判定"就直接收尾。结论正确，但少了一道可核查的规则扫描；严格来说应在 `info_gap` 前仍跑一次规则检查，这是可以改进的点。
4. **模型曾出现一次幻觉**。首轮 a4 的 `block` 理由里写了"requested_time 16:00 无可用时段（available_slots 为空）"，而它根本没有调用 `query_slots`。已通过"结论只由观测事实拼装、模型理由仅存 trace"的方式修正；但这类问题在自由文本里无法彻底消除。
5. **规则引擎能力有限**。只支持 5 种 check 类型，复杂组合规则需要写代码，不是通用 DSL。
6. **没有真人确认回路**。`--approve` 是模拟"用户已同意"，不是真实审批系统。
7. **单日单时区**。跨天排期、夏令时、多平台时区都不在范围内。

## 8. 复现步骤

```bash
cd material-scheduler
rm -rf runtime

# 全部自动化测试
python3 -m unittest discover -s tests -t . -v

# 六条必验（任选）
python3 main.py --input examples/a1-normal.input.json
python3 main.py --input examples/a2-conflict.input.json
python3 main.py --input examples/a3-info-gap.input.json
python3 main.py --input examples/a4-rule-risk.input.json
python3 main.py --input examples/a6-save-failed.input.json

# 重复提交：连跑两次，第二次 draft_status=duplicate
python3 main.py --input examples/a2-conflict.input.json --out /tmp/1.json
python3 main.py --input examples/a2-conflict.input.json --out /tmp/2.json

# 真实模型（需 .env 中的 key）
python3 main.py --input examples/a3-info-gap.input.json --mode llm
```
