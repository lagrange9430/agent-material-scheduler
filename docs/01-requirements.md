# 01 需求说明：素材审核与发布排期 Agent

> 题目：项目题一 · 素材审核与发布排期 Agent
> 本文档定义"做什么"和"怎么判定做完了"，不定义实现细节（实现见 `02-solution.md`）。

---

## 1. 背景与用户

内容运营每天要处理一批待发布素材。现状问题不是"没有排期工具"，而是三件事同时没答案：这条素材**能不能发**（授权、合规）、**什么时候发**（时段占用）、**还差什么**（缺字段、缺确认）。

**用户**：内容运营同学（非技术），一次提交 1~N 条素材。
**目标**：一次提交后拿到每项素材的审核状态、风险原因、建议时段、待确认项、草稿保存状态；排期之间不冲突；所有偏离用户原始要求的动作都显式可确认。

**用户不是**：审核员（不做人工终审）、发布系统（不触达任何外部分发渠道）。

---

## 2. 目标与成功标准

### 2.1 业务目标

| 编号 | 目标 |
|---|---|
| G1 | 给定素材 ID 列表，找出每条素材当前能不能进入排期 |
| G2 | 形成当天无时段冲突的排期建议 |
| G3 | 风险可解释：每条风险都能指到具体规则 ID 和原文证据 |
| G4 | 只产出草稿，永不发布；输出中不存在"已发布"语义 |
| G5 | 未经用户确认，不改变其硬性发布要求；偏离必须留痕并标记待确认 |

### 2.2 可判定的成功标准

| 编号 | 标准 | 判定方式 |
|---|---|---|
| S1 | 10 个验收用例（A1~A9 + C1，见 §10）全部通过 | `python -m unittest` 全绿 + `examples/` 有实际运行记录 |
| S2 | 同一任务重复执行，草稿数量不增加 | 跑两次，`runtime/drafts.json` 中草稿数仍为 1，第二次 `draft_status=duplicate` |
| S3 | 输出与代码中不存在发布能力 | 测试扫描输出 JSON：无 `published`/`已发布` 字段与文案；工具注册表无 `publish` |
| S4 | 触碰步数上限时返回 `incomplete` 且保留已完成结论 | 用例 A8 断言中间结论（已查到的风险/冲突）仍在输出里 |
| S5 | 每条建议时段都能追溯到工具返回 | 输出 `slot_evidence` 字段引用 `query_slots` 的返回摘要 |
| S6 | 无真实密钥入库 | 仓库内 `git grep -i "sk-"` 为空；`.env` 在 `.gitignore` 中 |

---

## 3. 范围

### 3.1 范围内

1. 素材读取（含"找不到"和"字段缺失"两种失败语义）
2. 规则检查（5 条合成规则，逐条给出可判定/不可判定）
3. 时段查询（单日、整点粒度、返回占用与可用）
4. 替代时段推荐（冲突时的有序候选）
5. 排期草稿保存（幂等、唯一写操作）
6. 任务级状态汇总与未完成原因
7. 决策轨迹落盘（每步动作、参数、返回摘要、依据）
8. 用户确认路径（`--approve`，离线可复现）

### 3.2 范围外（明确不做，避免过度设计）

| 不做 | 原因 |
|---|---|
| 真实发布 / 对接分发平台 | 题目硬性禁止，且属关键缺陷 |
| 审批流、审核员工单系统 | 题目未要求，不属于单 Agent 职责 |
| 多用户并发、权限体系 | 无业务输入，纯工程负担 |
| 跨天排期、时区换算 | 固定单日 `Asia/Shanghai`，避免引入不可控误差 |
| Web UI / HTTP 服务 / 线上部署 | 题目明确"非必需"，CLI 足够验证 |
| 多 Agent 协作 | 题目明确"非必需"，且会稀释单 Agent 决策的可见性 |
| 素材上传、转码、内容理解（图像/视频） | 素材以元数据形式给出 |

---

## 4. 输入契约

任务输入为单个 JSON 文件（`examples/*.input.json`）：

```json
{
  "task_id": "demo-m01-conflict",
  "date": "2026-11-10",
  "materials": [
    {
      "material_id": "M01",
      "requested_time": "18:00",
      "time_constraint": "preferred",
      "note": "新品开箱，希望晚间发布"
    }
  ],
  "options": {
    "max_steps": 12,
    "max_tool_calls": 8,
    "approve": []
  }
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `task_id` | 是 | 任务标识，写入 trace 与草稿 |
| `date` | 是 | 排期日期，`YYYY-MM-DD`；非法日期由工具返回错误 |
| `materials[].material_id` | 是 | 素材 ID，对应 `data/materials.json` |
| `materials[].requested_time` | 否 | 用户期望时段 `HH:00`；缺省表示"无偏好，由 Agent 选最早可用" |
| `materials[].time_constraint` | 否 | `hard` 或 `preferred`，缺省 `preferred`。`hard` 表示不可替代 |
| `options.max_steps` | 否 | 默认 12，上限 20 |
| `options.max_tool_calls` | 否 | 默认 8，上限 20 |
| `options.approve` | 否 | 形如 `["M01=19:00"]`，模拟用户已确认的替代时段 |

**校验规则**：`requested_time` 必须是整点且在 `09:00~21:00`；`time_constraint=hard` 时必须有 `requested_time`，否则视为输入非法，任务直接 `failed`（输入错误不计入 Agent 步数）。

---

## 5. 输出契约

### 5.1 任务级

```json
{
  "task_id": "demo-m01-conflict",
  "status": "needs_confirmation",
  "mode": "offline_mock",
  "steps_used": 4,
  "tool_calls_used": 4,
  "limits": {"max_steps": 12, "max_tool_calls": 8},
  "materials": [ ... ],
  "trace_path": "examples/trace/demo-m01-conflict.jsonl",
  "incomplete_reason": null
}
```

### 5.2 素材级

```json
{
  "material_id": "M01",
  "review_status": "pass",
  "risk_reasons": [],
  "requested_slot": "2026-11-10T18:00",
  "suggested_slot": "2026-11-10T19:00",
  "slot_status": "conflict_resolved",
  "slot_evidence": "query_slots(2026-11-10, 45min): 18:00 occupied by M00; 19:00 available",
  "needs_user_confirmation": true,
  "pending_items": ["确认把 18:00 调整为 19:00（原时段已被 M00 占用）"],
  "draft_status": "saved",
  "draft_id": "D-7f3a9c21",
  "deviation": {"from": "18:00", "to": "19:00", "reason": "requested slot occupied"}
}
```

### 5.3 状态枚举（穷举，不允许新增）

**`review_status`**

| 值 | 含义 |
|---|---|
| `pass` | 已读、规则全部可判定且通过 |
| `risk` | 至少一条规则判为违规 |
| `info_gap` | 关键字段缺失，规则无法判定，不能给出"可以发"的结论 |

**`slot_status`**

| 值 | 含义 |
|---|---|
| `available` | 期望时段空闲 |
| `conflict_resolved` | 期望时段被占用，已找到可用替代 |
| `conflict_unresolved` | 冲突且无可用替代（或硬约束不允许替代） |
| `not_checked` | 因信息缺失/规则阻断，未进入排期环节 |

**`draft_status`**

| 值 | 含义 |
|---|---|
| `saved` | 本次新建草稿 |
| `duplicate` | 幂等命中，草稿已存在，未新建 |
| `updated` | 同一份草稿因用户确认改期而就地更新（`revision+1`），未新建 |
| `failed` | 保存被调用但失败，无草稿 |
| `not_saved` | 未调用保存（阻断/缺信息/未确认） |

**`status`（任务级）**

| 值 | 含义 |
|---|---|
| `completed` | 所有素材都有确定结论，无待确认项 |
| `needs_confirmation` | 存在必须用户确认才能继续的动作（改期、替代方案） |
| `blocked` | 全部素材被规则阻断或硬约束不可满足 |
| `incomplete` | 触碰步数/工具调用上限 |
| `failed` | 输入非法、关键写操作失败（保存失败）或决策层不可用 |

> **禁止项**：不存在 `published`、`scheduled_live`、`live` 等任何"已发布"状态。草稿是排期的最高成熟度。

---

## 6. 工具契约

只有 4 个工具，全部无网络、无副作用（`save_draft` 除外，其副作用仅限本地草稿文件）。

| 工具 | 入参 | 成功返回 | 错误码 |
|---|---|---|---|
| `read_material` | `material_id` | `{ok:true, material:{...}, missing_fields:[]}` | `NOT_FOUND`、`MISSING_FIELDS` |
| `check_rules` | `material_id` | `{ok:true, violations:[...], skipped:[...]}` | `RULEBOOK_UNAVAILABLE` |
| `query_slots` | `date`, `duration_min`, `exclude_occupied` | `{ok:true, occupied:[...], available:[...]}` | `INVALID_DATE` |
| `save_draft` | `draft`, `idempotency_key` | `{ok:true, draft_id, revision, created:bool}` | `SAVE_FAILED`、`CONFLICT_SLOT` |

**约定**

- 每个工具返回都带 `ok`，Agent 必须分支处理 `ok=false`。
- `save_draft` 是唯一写操作；代码库中不存在发布类工具或函数。
- 工具失败**不自动重试**。重试属于"固定流程"，不构成决策；失败即如实上报，由模型决定改方案还是终止。
- `check_rules` 对缺字段的规则返回 `skipped[{rule_id, reason:"missing_field:xxx"}]`，而不是猜测通过。

---

## 7. 规则库（合成，5 条）

| ID | 名称 | 检查内容 | 严重级 | 是否阻断排期 |
|---|---|---|---|---|
| R01 | 版权授权 | `copyright.status == "granted"` 且 `copyright.scope` 覆盖目标平台、未过期 | blocker | 是 |
| R02 | 绝对化用语 | 标题/文案含"最"、"第一"、"国家级"、"顶级"等禁用词 | blocker | 是 |
| R03 | 时长上限 | `duration_seconds <= 60`（平台单条上限） | major | 是 |
| R04 | 同素材重复排期 | 该素材在 `data/schedule.json` 中当天已被排过 | major | 是 |
| R05 | 广告标识 | `category` 属于 `ads`/`medical` 时，`has_ad_label` 必须为 `true` | major | 是 |

规则数据放 `data/rules.json`，代码不硬编码规则条文（便于评审直接改规则观察行为变化）。

---

## 8. 数据字典

### 8.1 `data/materials.json`（6 条）

| ID | 设计意图 | 关键字段 | 服务于用例 |
|---|---|---|---|
| M01 | 无风险，但要求时段被占用 | `duration_seconds=45`，授权 OK，`requested=18:00` | A2 时段冲突 / C1 对照 |
| M02 | 完全正常 | `duration_seconds=30`，`requested=10:00` | A1 正常排期 |
| M03 | 关键字段缺失 | 缺 `duration_seconds`、缺 `copyright.scope` | A3 信息缺失 |
| M04 | 规则违规 | 无授权 + 标题含"全网最低" | A4 规则风险 |
| M05 | 正常，但保存被注入失败 | 与 M02 同构，`requested=11:00` | A6 保存失败 |
| M06 | 硬约束 + 冲突 | `time_constraint=hard`，`requested=20:00`，20:00 已占用 | A7 硬约束阻断 |

### 8.2 `data/schedule.json`（当天已有排期）

占用：`14:00`（M08）、`18:00`（M00）、`20:00`（M07）。
其余整点 `09:00~21:00` 可用。这样 M01 的 19:00 天然可用，无需额外构造。

### 8.3 `data/rules.json`

包含 R01~R05 的 `id / name / field / expect / severity / blocks_schedule / message / evidence_hint`。

---

## 9. 决策要求（ReAct 的判定口径）

### 9.1 动作空间（模型每步只能选一个）

`read_material` / `check_rules` / `query_slots` / `save_draft` / `request_confirmation` / `block` / `finish_material`

### 9.2 循环终止条件

| 条件 | 结果 |
|---|---|
| 所有素材处理完且无需确认 | `completed` |
| 存在待确认项（改期、替代方案） | `needs_confirmation` |
| 全部素材不可发 | `blocked` |
| `steps_used >= max_steps` 或 `tool_calls_used >= max_tool_calls` | `incomplete` + 已完成结论 |
| 输入非法 / 规则库不可读 | `failed` |

### 9.3 决策必须依赖工具结果（防"假 ReAct"）

同一输入、不同工具返回，动作必须不同。要求至少构造一组**受控对照**：

- C1-a：M01 要求 18:00，`schedule.json` 中 18:00 被占用 → 动作序列包含"查时段 → 选 19:00 → 请求确认"，不保存为已确认。
- C1-b：同一 M01，仅把 18:00 改为空闲 → 动作序列"查时段 → 直接保存"，无待确认项。

评审可只改 `data/schedule.json` 一个字段来复现差异，这是"模型看结果做决定"最硬的证据。

### 9.4 用户确认边界

- `preferred`：可以建议替代时段，但必须 `needs_user_confirmation=true`，且 `deviation` 留痕。
- `hard`：**不允许**替代。冲突即 `blocked`，且不得保存草稿。
- 用户通过 `options.approve` 给出同意后，该素材的替代时段才转为 `saved` 且 `needs_user_confirmation=false`。

### 9.5 信息缺失处理

不猜、不默填。输出 `info_gap` + 缺失字段清单 + "该字段影响哪条规则/哪个决策"，并给出补料后可直接重跑的提示。

### 9.6 工具失败处理

以 `read_material` 失败为例：若 `NOT_FOUND`，该素材直接 `blocked`，不影响其他素材继续处理（部分成功必须保留）。若 `save_draft` 失败，该素材 `draft_status=failed`，任务 `status` 不允许是 `completed`。

---

## 10. 验收矩阵

| 编号 | 场景 | 输入 | 期望关键结论 | 产物 |
|---|---|---|---|---|
| A1 | 正常排期 | `a1-normal.input.json`（M02） | `review_status=pass`，`slot_status=available`，`draft_status=saved`，任务 `completed` | `a1-normal.output.json` + trace |
| A2 | 时段冲突 | `a2-conflict.input.json`（M01） | `slot_status=conflict_resolved`，建议 19:00，`needs_user_confirmation=true`，任务 `needs_confirmation`，**不出现"已发布"** | `a2-conflict.output.json` + trace |
| A3 | 素材信息缺失 | `a3-info-gap.input.json`（M03） | `review_status=info_gap`，列出缺失字段与受影响的规则，`draft_status=not_saved` | 同上 |
| A4 | 规则风险 | `a4-rule-risk.input.json`（M04） | `review_status=risk`，`risk_reasons` 含 R01/R02 及原文证据，`blocked` | 同上 |
| A5 | 重复提交 | 连续执行 A2 两次 | 第二次 `draft_status=duplicate`，草稿文件仅 1 条，`draft_id` 相同 | `a5-duplicate.output.json` + 草稿快照 |
| A6 | 保存失败 | `a6-save-failed.input.json`（M05，注入失败） | `draft_status=failed`，`ok=false`，原因可见，无 `draft_id` | 同上 |
| A7 | 硬约束冲突（加分） | `a7-hard-block.input.json`（M06） | `blocked`，说明"20:00 被占用且不可改期"，不保存草稿 | 同上 |
| A8 | 步数上限（加分） | `a8-step-limit.input.json`（`max_steps=2`） | `incomplete`，`incomplete_reason` 指明卡点，已获得的风险信息保留 | 同上 |
| A9 | 批量部分成功（加分） | `a9-batch.input.json`（M02/M04/M01） | 已成功的草稿不因其它素材阻断而丢失 | `a9-batch.output.json` + trace |
| A10 | 用户确认改期（加分） | `a2-conflict.input.json --approve M01=19:00` | `draft_status=updated`，`revision=2`，草稿数仍为 1 | `a2-approved.output.json` |
| C1 | 受控对照 | A2 的输入 + 空闲 schedule | 动作序列变化，无需确认 | `c1-control-*.output.json` |

**最低交付**：A1~A6 + 两条执行路径 trace；**实际交付**：A1~A10 + C1（含 mock 与真实模型两轮）。

---

## 11. 非功能要求

| 要求 | 说明 |
|---|---|
| 运行环境 | Python 3.11+，**仅标准库**（真实模型调用用 `urllib`），`pip install -r requirements.txt` 后无额外依赖 |
| 可复现 | `AGENT_MODE=mock` 下相同输入 → 字节级相同输出（trace 行序稳定） |
| 无密钥泄漏 | `.env` 入 `.gitignore`；日志与 trace 不含 `Authorization` 头；输出不含密钥 |
| 响应时间 | mock 模式全用例 < 5s |
| 可读性 | CLI 输出为中文；JSON 字段名为英文；错误信息必须包含"原因 + 下一步建议" |
| 幂等 | 幂等键 = `sha256(material_id + date + proposed_slot)`，落盘可跨进程 |

---

## 12. 模型路径与 Mock 边界（必须如实声明）

| 模式 | 决策来源 | 能证明什么 | 不能证明什么 |
|---|---|---|---|
| `mock` | 确定性策略函数（if 规则 = 决策） | 流程正确、状态机正确、工具契约正确、幂等与失败处理正确 | **不能**证明真实模型的决策质量与泛化能力 |
| `llm` | 真实模型逐步输出 JSON 动作 | 真实 ReAct 循环可用、模型能按工具返回调整动作 | 单次样本不代表稳定性 |

交付物同时提供两种模式的入口；`examples/` 中的记录会明确标注 `mode` 字段。默认 `auto`：有 `DEEPSEEK_API_KEY` 走 `llm`，否则回落 `mock` 并在输出中标注。

---

## 13. 术语

| 术语 | 定义 |
|---|---|
| 草稿 | 排期建议的持久化记录，状态最高到"待确认/已存草稿"，不代表发布 |
| 待确认 | 需要用户显式同意才能执行的动作（如改时段、采用替代方案） |
| 阻断 | 规则或硬约束导致该素材不能进入排期 |
| 硬约束 | 用户声明不可更改的发布时间（`time_constraint=hard`） |
| 幂等键 | 判定"是否同一份草稿"的稳定标识 |
| 部分成功 | 一个任务内部分素材成功、部分失败时，成功结论必须保留 |
