# 02 方案设计：素材审核与发布排期 Agent

> 需求口径见 `01-requirements.md`。本文回答三个问题：**怎么跑**、**模型在哪一步做决定**、**哪些取舍是故意的**。

---

## 1. 设计约束（先定边界，再谈结构）

| 约束 | 来源 | 对设计的影响 |
|---|---|---|
| 只有草稿，没有发布 | 题目硬性 | 工具表里不存在 publish；状态机没有"已发布"态；测试扫描输出文案 |
| 必须真循环 | 题目硬性 | 决策函数读"观测"再出动作；真实模型路径与 mock 路径共用同一循环 |
| 必须有上限 | 题目硬性 | 每素材独立预算 + 任务总闸，双保险 |
| 零依赖可跑 | 评审体验 | 仅标准库；真实模型用 `urllib`，不引 SDK |
| 可复现 | 评审对照实验 | mock 模式输出稳定；随机性全部消除（无 temperature、无时间戳进正文） |
| 密钥不入库 | 安全 | `.env` 忽略；日志脱敏；测试里有反例断言 |

---

## 2. 架构总览

```
             ┌──────────────────────────────────────────────┐
  输入 JSON  │  cli.py   参数解析 / 模式选择 / 结果渲染       │
  ──────────▶│                                              │
             └───────────────┬──────────────────────────────┘
                             │
                    ┌────────▼────────┐
                    │   agent.py      │  ReAct 主循环
                    │  ┌───────────┐  │  · 预算记账（步/工具）
                    │  │ policy    │◀─┼── 决策：下一步做什么
                    │  └─────┬─────┘  │      mock: 观测驱动的策略函数
                    │        │        │      llm : 模型输出一个动作 JSON
                    │  ┌─────▼─────┐  │
                    │  │ executor  │──┼──▶ tools.py（4 个工具，唯一副作用是草稿）
                    │  └─────┬─────┘  │
                    │        │        │
                    │  ┌─────▼─────┐  │
                    │  │  observe  │  │  结果写回 state（含失败语义）
                    │  └───────────┘  │
                    └────────┬────────┘
                             │
              ┌──────────────┼───────────────┐
              ▼              ▼               ▼
        trace.py        store.py        models.py
      JSONL 轨迹      JSON 读写/幂等    实体与枚举
```

**关键分层意图**：`policy` 只回答"下一步做什么"，`tools` 只回答"做什么会发生什么"。两者通过 `state.observations` 解耦——这正是换模型不改流程、换流程不改工具的原因，也是评审能单独替换 policy 做对照实验的原因。

---

## 3. 目录与模块职责

```
material-scheduler/
├── main.py                      # 入口垫片：sys.path 注入 + 调 cli.main()
├── src/scheduler_agent/
│   ├── __init__.py
│   ├── __main__.py              # 支持 python -m scheduler_agent
│   ├── models.py                # Material/Draft/Action/Observation/枚举
│   ├── config.py                # .env 读取、模式解析（auto/mock/llm）
│   ├── store.py                 # data/*.json 读取、runtime/drafts.json 幂等写
│   ├── tools.py                 # ToolRegistry + 4 个工具 + 错误码 + 故障注入
│   ├── policy.py                # MockPolicy：观测 → 动作（确定性）
│   ├── llm.py                   # DeepSeekClient：prompt 构造 / JSON 校验 / 超时
│   ├── agent.py                 # 循环、预算、收敛判定、部分成功
│   ├── trace.py                 # JSONL 写入与后处理（补 next_action）
│   └── cli.py                   # argparse、中文渲染、退出码
├── data/                        # materials/rules/schedule（合成数据）
├── examples/                    # 输入、输出、trace、测试结果
├── tests/                       # unittest
└── docs/
```

---

## 4. 动作协议

所有动作统一为：

```json
{"action": "query_slots", "args": {"date": "2026-11-10", "duration_min": 1}, "reason": "确认 18:00 是否可用"}
```

| 动作 | 类别 | 预算消耗 | 说明 |
|---|---|---|---|
| `read_material` | 工具 | 1 步 + 1 工具调用 | 读素材 |
| `check_rules` | 工具 | 1 步 + 1 工具调用 | 规则检查 |
| `query_slots` | 工具 | 1 步 + 1 工具调用 | 查时段 |
| `save_draft` | 工具 | 1 步 + 1 工具调用 | **唯一写操作** |
| `request_confirmation` | 终止 | 1 步 | 停止推进，输出待确认项 |
| `block` | 终止 | 1 步 | 阻断，说明原因 |
| `finish_material` | 终止 | 1 步 | 该素材处理完毕 |

**终止 ≠ 任务结束**：一个素材终止后，循环切到下一条素材；任务在"全部素材终止"或"预算耗尽"时收敛。

---

## 5. 主循环

```python
for material in task.materials:
    state = MaterialState(material)
    for step in range(budget.max_steps):
        action = policy.next(state)            # ★ 决策点
        trace.record(step, action, state)      # 记录动作与依据
        if action.is_tool:
            if budget.tool_calls_exhausted:
                return incomplete(state, "tool_budget")
            result = tools.call(action, state)  # ★ 执行
            state.observe(result)               # ★ 观察（含 ok=False）
            budget.tool_calls += 1
        else:
            state.finish(action)                # 终止动作
            break
    else:
        return incomplete(state, "step_budget") # ★ 撞线，保留已有结论
```

三处 ★ 就是题目要求的"模型选动作 → 执行工具 → 观察结果 → 调整"。`state.observe()` 不是简单追加日志：它会把 `ok=False`、`missing_fields`、`violations`、`available` 解析成结构化事实，供下一次决策读取。

---

## 6. 决策表（观测 → 下一个动作）

这张表描述的是 **mock 策略**（也作为真实模型的系统提示骨架）。注意每一行左侧都是"上一次工具**实际返回**"，不是预设流程的第 N 步：

| # | 当前观测 | 下一个动作 | 依据 |
|---|---|---|---|
| 1 | 尚无任何观测 | `read_material` | 不知道素材内容，任何判断都是猜 |
| 2 | `read_material` 返回 `NOT_FOUND` | `block` | 没有素材就没有排期对象 |
| 3 | 读到素材，含 `missing_fields` | `check_rules` | 先看哪些规则**仍可判定**，避免把"部分可知"误报成"全部未知" |
| 4 | 规则有 `blocker` 违规 | `block` | 合规不可绕过，不进排期环节 |
| 5 | 规则有 `skipped`（缺字段）且无违规 | `finish_material(info_gap)` | 不能把"没查出问题"当成"没问题" |
| 6 | 规则全通过 | `query_slots` | 合规已确认，才值得占用时段 |
| 7 | 期望时段 `available` | `save_draft` | 无冲突，直接落草稿 |
| 8 | 期望时段被占用且 `hard` | `block` | 硬约束不可改，替代方案等于擅自变更需求 |
| 9 | 期望时段被占用且 `preferred`，有替代 | `save_draft`(替代时段, 待确认) → `request_confirmation` | 可建议但不可默认，必须留痕待确认 |
| 10 | 期望时段被占用且无任何替代 | `request_confirmation` | 没有可行解，把选择权交回用户 |
| 11 | `save_draft` 返回 `SAVE_FAILED` | `finish_material(save_failed)` | 失败不自动重试（重试是固定流程，不是决策），如实上报 |
| 12 | `save_draft` 返回 `created=false` | `finish_material(duplicate)` | 幂等命中，不视为新草稿，也不视为失败 |
| 13 | `save_draft` 返回 `updated=true` | `finish_material(approved)` | 用户确认改期后草稿就地升版（revision+1），不新建第二份 |

第 5 行和第 7 行的差异，就是"看了结果再决定"的直接证据：同样的素材，只因为 `check_rules` 的 `skipped` 非空，动作从"查时段"变成"报缺料"。

---

## 7. 冲突消解算法

```
candidates = [s for s in slots.available if s + duration <= day_end]
if requested in slots.available:            -> available
if constraint == "hard":                    -> block
if not candidates:                          -> conflict_unresolved
else: 取 min(candidates, key=lambda s: (0 if s > requested else 1, abs(s - requested)))
```

排序规则：**优先晚于原时段的最近整点，其次早于原时段的最近整点**。

理由：发布时段通常与流量高峰绑定，往后挪比往前挪更安全（避开已经开始的时段，也不影响预热节奏）。这条规则写在 `02` 里，是因为它属于产品判断，不是技术必然——评审若不同意，改一行 key 即可。

无 `requested_time` 时（用户没偏好）：直接取最早可用时段。

---

## 8. 确认边界

> 另有一道**硬约束护栏**：即使决策层给出 `save_draft(slot≠requested)` 而 `time_constraint=hard`，
> `agent._guard()` 也会把它改写成 `block`。护栏不依赖模型是否听话，测试里用 `_ReschedulePolicy` 验证。

三层，缺一不可：

1. **数据层**：草稿必须同时存 `requested_slot` 与 `proposed_slot`，不等时 `deviation.reason` 必填。
2. **语义层**：`needs_user_confirmation=true` 时，任务 `status` 不得为 `completed`，只能是 `needs_confirmation`。
3. **重放层**：`--approve M01=19:00` 让"用户已同意"成为**输入**而不是隐式状态，于是"同意后改期"这条路径可以离线复现、可以被测试断言。

没有第三层，"用户同意"就只能靠嘴说；有了第三层，同一条素材的两种命运（待确认 / 已确认）可以并排跑给评审看。

---

## 9. 幂等与草稿存储

**幂等键**：`sha256(f"{material_id}|{date}|{proposed_slot}")[:12]`，草稿 ID 取前 8 位加 `D-` 前缀。

**存储**：`runtime/drafts.json`

```json
{
  "D-7f3a9c21": {
    "draft_id": "D-7f3a9c21",
    "key": "7f3a9c21a1b2",
    "task_id": "demo-m01-conflict",
    "material_id": "M01",
    "date": "2026-11-10",
    "proposed_slot": "19:00",
    "requested_slot": "18:00",
    "status": "pending_confirmation",
    "revision": 1,
    "created_at": "2026-11-09T22:10:00+08:00"
  }
}
```

**写路径**：`save_draft` 先校验该时段仍未占用（防并发/防绕过），再按 key 查表。三分支：

| 情况 | 返回 | 效果 |
|---|---|---|
| key 不存在 | `created=true` | 新建，`revision=1` |
| key 存在且 `needs_confirmation` 相同 | `created=false` | 幂等命中，不改写 |
| key 存在且由「待确认」变为「已确认」（`--approve`） | `created=false, updated=true` | 就地更新为 `draft_confirmed`，`revision+1` |

第三行是刻意设计：用户确认改期时，**不新建第二份草稿**，而是给同一份草稿升版。这样"重复提交只有一份"与"确认后状态要变"两个要求可以同时成立。反向的降级（已确认 → 待确认）被明确拒绝，避免一次重跑把用户刚确认的排期打回去。

**为什么落盘而不是放内存**：内存幂等在"进程重启后重复提交"面前是假的。落盘让"同一请求只产生一份草稿"这条验收可以被独立复现——评审删掉草稿文件重跑，结果应当与第一次一致。

---

## 10. 失败与部分成功

| 失败类型 | 处理 | 禁止 |
|---|---|---|
| 工具返回 `ok=false` | 记录结构化错误，进入决策表第 11 行 | 自动重试、把失败当成功 |
| 单素材失败 | 该素材标记失败，**继续处理下一条** | 整单回滚、丢掉已成功素材的草稿 |
| 步数/工具预算耗尽 | 任务 `incomplete`，输出已完成结论 | 静默截断、假装完成 |
| 输入非法 | 任务 `failed`，不消耗预算 | 进入循环后再报错 |

**注入方式**：输入 JSON 的 `faults: ["M05:save_draft"]`，工具层按 `material_id:tool_name` 精确注入，返回 `SAVE_FAILED`。这比 monkeypatch 更透明——评审看输入文件就知道哪一步会坏。

---

## 11. 真实模型接入

| 项 | 取值 |
|---|---|
| 端点 | `https://api.deepseek.com`（OpenAI 兼容 `/chat/completions`） |
| 模型 | `deepseek-flash` |
| 参数 | `temperature=0`、`response_format={"type":"json_object"}`、`max_tokens=256` |
| 超时 | 60s，超时按 `failed` 处理并写明原因 |

**Prompt 结构**（每步重发，无状态漂移）：

1. 角色与硬规则（只能选 7 个动作之一；只能使用已返回的可用时段；禁止声称已发布）
2. 工具签名与参数示例
3. 当前素材的**完整观测序列**（压缩后的 JSON：已读元数据、规则结论、时段表、草稿结果）
4. 剩余预算（步数/工具调用）
5. 输出 schema：`{"action","args","reason"}`

**校验与纠错**：解析失败或动作名不在白名单 → 允许一次"格式纠错"（把错误信息回填，要求重出），计入步数但**不消耗工具调用**；仍失败则任务 `failed`。工具调用本身**永不自动重试**——这两件事必须分开，否则"重试"会掩盖"模型没在决策"。

**成本控制**：只传压缩观测（截断到 1200 字），不传全文素材；单次任务预计 5~8 次调用，`deepseek-flash` 下单次任务成本可忽略。

---

## 12. Trace 规范

`examples/trace/<task_id>.jsonl`，一行一步：

```json
{"step":3,"material_id":"M01","action":"query_slots",
 "args":{"date":"2026-11-10","duration_min":1},
 "ok":true,"result_summary":"18:00 occupied by M00; available 09,10,11,12,13,15,16,17,19,21",
 "rationale":"规则已通过，需要确认 18:00 是否可用",
 "next_action":"save_draft"}
```

- `result_summary` 是**摘要**不是全文：保留可核查的关键事实（谁占用、哪些可用），便于人读。
- `rationale` 只写**决策依据**，不写思维链——题目明确不要求内部思维过程。
- `next_action` 由 trace 后处理补齐（看下一步实际动作），保证"后续动作"这一列真实而非自述。

任务结束追加一条汇总行：`{"event":"summary","status":...,"steps":...,"tool_calls":...,"drafts":[...]}`。

---

## 13. 测试设计

| 层次 | 文件 | 断言什么 |
|---|---|---|
| 工具契约 | `test_tools.py` | 4 个工具正常/异常返回；注入故障精确命中；`save_draft` 拒绝占用时段 |
| 循环与预算 | `test_loop.py` | 步数上限触发 `incomplete` 且保留结论；工具调用上限独立生效；终止动作不消耗工具预算 |
| 幂等 | `test_idempotency.py` | 同输入两次 → 1 份草稿、`created=false`；不同时段 → 2 份 |
| 验收矩阵 | `test_acceptance.py` | A1~A8 + C1 逐条断言 |
| 反例/红线 | `test_safety.py` | 输出与源码无发布语义；无 `published` 字段；密钥不落盘；硬约束不被静默改期 |

测试用 `tmp_path` 隔离 `runtime/`，保证可重复执行（仓库里的 `runtime/` 不参与断言）。

---

## 14. 技术取舍（为什么不用框架）

| 决策 | 选择 | 理由 | 代价 |
|---|---|---|---|
| 框架 | **原生实现，不引 LangGraph/ADK** | 本题只有一个 Agent、4 个工具、7 个动作，框架的图编排收益趋近于零，反而让"决策发生在哪一行"变模糊。题目明说框架不加分，能解释取舍才加分 | 需要自己写预算、状态、重试边界 |
| 运行形态 | **CLI** | 评审一条命令可跑，无端口、无进程管理、无并发干扰 | 不能多人同时用（不在范围） |
| 依赖 | **零第三方** | 环境差异是评审跑不通的头号原因；`urllib` 足以调 OpenAI 兼容接口 | 没有 SDK 的类型提示与重试封装 |
| 决策来源 | **mock + llm 双实现** | mock 保证"任何环境都能跑通并验证状态机"；llm 保证"决策确实是模型做的" | 两套 policy 需要共享同一状态契约 |
| 幂等 | **落盘** | 能独立复现"重复提交" | 需要处理并发写（单进程场景下用文件锁略过，已记入限制） |
| 替代时段 | **晚优先** | 发布节奏通常向后顺延更安全 | 不是唯一正确答案，属产品判断 |

---

## 15. 已知限制（诚实清单）

1. **单进程**：`drafts.json` 无文件锁，多进程并发写会丢更新。生产化需要换成 SQLite 或加锁。
2. **单日排期**：不支持跨天、不支持时区换算，`date` 之外的日期一律按非法处理。
3. **规则引擎是声明式的，不是通用 DSL**：R01~R05 靠 `rules.json` 的字段路径匹配，复杂规则（如组合条件）需要写代码。
4. **mock 不证明决策质量**：它只能证明流程与状态机正确；真实模型的效果依赖 `llm` 模式，且单次 trace 不构成稳定性证据。
5. **没有真人确认回路**：`--approve` 是模拟用户已同意，不是真的审批系统对接。
6. **素材是元数据**：不解析图像/视频内容，因此"画面是否合规"不在检查范围。
