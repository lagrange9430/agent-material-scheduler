# 素材审核与发布排期 Agent

内容团队提交一批待发布素材，Agent 逐条完成**审核 → 查时段 → 存草稿**，输出每项素材的审核状态、风险原因、建议时段、待确认项与草稿保存状态。

> **只保存草稿，不发布。** 代码里没有发布工具，输出里没有"已发布"，草稿是排期的最高成熟度。

- 题目：项目题一 · 素材审核与发布排期 Agent
- 运行形态：CLI（零第三方依赖，`python main.py` 即可）
- 模型：`deepseek-flash`（OpenAI 兼容接口），**无 key 自动回落离线 mock**

---

## 1. 快速开始

```bash
cd material-scheduler

# 1) 不需要模型，直接跑（推荐先跑这个）
python3 main.py --input examples/a2-conflict.input.json --mode mock

# 2) 跑全部测试
python3 -m unittest discover -s tests -t . -v

# 3) 用真实模型跑（需 .env 中的 key，见 §2）
python3 main.py --input examples/a2-conflict.input.json --mode llm
```

> 不加 `--mode` 时为 `auto`：`\.env` 里有 key 就走真实模型（`mode` 显示 `llm:deepseek-flash`），
> 没有 key 自动回落 `offline_mock`。想稳定复现下面的样例输出，显式加 `--mode mock`。

第一条命令的输出（真实执行，`offline_mock`）：

```
任务 a2-conflict｜状态 待用户确认（needs_confirmation）｜模式 offline_mock
步数 5｜工具调用 4｜单素材上限 8/6

[M01] 审核 pass｜时段 conflict_resolved｜草稿 saved
    建议时段 2026-11-10T19:00（原 18:00，原因 requested slot occupied）
    待确认：确认把 18:00 调整为 19:00（原时段已被占用）
    草稿：D-436a4e79
    结论：已给出建议时段，等待用户确认后才能定稿，未发布
```

## 2. 环境与配置

| 项 | 要求 |
|---|---|
| Python | 3.11+（仅标准库，无需 `pip install`） |
| 网络 | mock 模式完全离线；llm 模式需要访问模型端点 |
| 密钥 | 可选。没有 `DEEPSEEK_API_KEY` 时自动使用 `offline_mock` |

```bash
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY=***
```

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 空 | 为空则回落 mock |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容端点 |
| `MODEL_NAME` | `deepseek-flash` | 实测该账户仅支持 `deepseek-flash` / `deepseek-v4-pro` |
| `AGENT_MODE` | `auto` | `auto` / `mock` / `llm` |
| `MODEL_TIMEOUT` | `60` | 单次调用超时（秒） |

**连通性自检**（不打印密钥）：

```bash
python3 main.py --check-model
```

## 3. 目录结构

```
material-scheduler/
├── README.md
├── main.py                     # 入口：注入 src 到 sys.path 后调 CLI
├── requirements.txt            # 运行时零依赖，仅说明
├── .env.example                # 所需变量
├── src/scheduler_agent/
│   ├── models.py               # 实体 + 状态枚举（穷举）
│   ├── config.py               # .env 解析与模式选择
│   ├── store.py                # 数据读取 + 草稿幂等存储
│   ├── tools.py                # 4 个工具（唯一写操作 save_draft）
│   ├── policy.py               # mock 决策：观测 → 动作
│   ├── llm.py                  # 真实模型：prompt / JSON 校验 / 纠错
│   ├── agent.py                # ReAct 循环 + 预算 + 结果组装
│   ├── trace.py                # JSONL 轨迹
│   └── cli.py                  # 参数、渲染、退出码
├── data/
│   ├── materials.json          # 6 条合成素材
│   ├── rules.json              # 5 条审核规则
│   ├── schedule.json           # 当天已有排期（18:00 被占）
│   └── schedule_alt.json       # 对照实验用（18:00 空闲）
├── examples/                   # 输入、实际输出、trace、测试结果
├── tests/                      # 40 个 unittest 用例
└── docs/                       # 01~05
```

## 4. 运行与评审入口

### 4.1 六条必验 + 加分项

| 场景 | 命令 | 期望状态 |
|---|---|---|
| 正常排期 | `python3 main.py --input examples/a1-normal.input.json` | `completed`，`draft_status=saved` |
| 时段冲突 | `python3 main.py --input examples/a2-conflict.input.json` | `needs_confirmation`，建议 19:00 |
| 信息缺失 | `python3 main.py --input examples/a3-info-gap.input.json` | `info_gap`，列出缺失字段 |
| 规则风险 | `python3 main.py --input examples/a4-rule-risk.input.json` | `blocked`，带规则 ID 与原文 |
| 重复提交 | 连跑两次 a2 | 第二次 `draft_status=duplicate` |
| 保存失败 | `python3 main.py --input examples/a6-save-failed.input.json` | `failed`，无 `draft_id` |
| 硬约束冲突 | `python3 main.py --input examples/a7-hard-block.input.json` | `blocked`，不改期 |
| 步数上限 | `python3 main.py --input examples/a8-step-limit.input.json` | `incomplete`，保留风险 |
| 批量部分成功 | `python3 main.py --input examples/a9-batch.input.json` | 成功项不因失败项丢失 |

### 4.2 受控对照实验（证明决策依赖工具返回）

```bash
python3 main.py --input examples/a2-conflict.input.json      # 18:00 被占 → 请求确认
python3 main.py --input examples/c1-control-free.input.json  # 18:00 空闲 → 直接保存
```

两次运行只差 `schedule_file` 一个选项，动作序列与结论都不同。

### 4.3 用户确认路径

```bash
python3 main.py --input examples/a2-conflict.input.json --approve M01=19:00
```

草稿就地更新为已确认（`revision 1 → 2`），不新建第二份。

### 4.4 退出码

| 码 | 含义 |
|---|---|
| 0 | `completed` / `needs_confirmation` / `blocked`（都是有效业务结论） |
| 2 | 配置错误 |
| 3 | `incomplete`（达到步数或工具调用上限） |
| 4 | `failed`（输入非法 / 保存失败 / 决策层不可用） |

## 5. 输出说明

```json
{
  "task_id": "a2-conflict",
  "status": "needs_confirmation",
  "mode": "offline_mock",
  "steps_used": 5,
  "tool_calls_used": 4,
  "materials": [{
    "material_id": "M01",
    "review_status": "pass",
    "requested_slot": "2026-11-10T18:00",
    "suggested_slot": "2026-11-10T19:00",
    "slot_status": "conflict_resolved",
    "needs_user_confirmation": true,
    "pending_items": ["确认把 18:00 调整为 19:00（原时段已被占用）"],
    "draft_status": "saved",
    "draft_id": "D-436a4e79",
    "deviation": {"from": "18:00", "to": "19:00", "reason": "requested slot occupied"},
    "conclusion": "已给出建议时段，等待用户确认后才能定稿，未发布"
  }],
  "trace_path": "examples/trace/a2-conflict.jsonl"
}
```

| 枚举 | 取值 |
|---|---|
| `review_status` | `pass` / `risk` / `info_gap` |
| `slot_status` | `available` / `conflict_resolved` / `conflict_unresolved` / `not_checked` |
| `draft_status` | `saved` / `duplicate` / `updated` / `failed` / `not_saved` |
| `status` | `completed` / `needs_confirmation` / `blocked` / `incomplete` / `failed` |

**不存在** `published` 或任何"已发布"状态。

## 6. 设计要点

1. **决策与执行分离**：`policy` 只回答"下一步做什么"，`tools` 只回答"做了会发生什么"，`agent` 只负责预算与收网。换模型不改流程，换流程不改工具。
2. **确认边界三层**：数据层存双时段 + 偏离原因；语义层待确认时任务不可能是 `completed`；重放层用 `--approve` 让"用户同意"成为可测输入。另有**硬约束护栏**：即使决策层想把 `hard` 素材改期，也会被拦截成 `blocked`。
3. **失败不重试、不美化**：工具失败只上报，不自动重试（重试是固定流程，不是决策）；结论由观测事实拼装，模型理由仅存 trace，避免幻觉进输出。

详见 [docs/02-solution.md](docs/02-solution.md)。

## 7. 已知限制

- 单进程：`runtime/drafts.json` 无文件锁，并发写会丢更新。
- 单日、单时区（`Asia/Shanghai`），不支持跨天排期。
- 规则引擎支持 5 种 check 类型，复杂组合规则需改代码。
- `--approve` 是模拟用户已同意，未接真实审批系统。
- llm 模式仅 6 个用例各跑 1 次，不构成稳定性证据。

完整清单见 [docs/03-evaluation.md](docs/03-evaluation.md) 第 7 节。

## 8. 文档索引

| 文档 | 内容 |
|---|---|
| [01-requirements.md](docs/01-requirements.md) | 用户、目标、范围、输入输出契约、状态枚举、验收矩阵 |
| [02-solution.md](docs/02-solution.md) | 架构、主循环、决策表、冲突消解、确认边界、技术取舍 |
| [03-evaluation.md](docs/03-evaluation.md) | 测试结果、验收实测、对照实验、失败与限制 |
| [04-ai-usage.md](docs/04-ai-usage.md) | AI 使用范围、AI 犯的 5 个错、验证清单 |
| [05-retrospective.md](docs/05-retrospective.md) | 最难的问题、学习、下一步 |

## 9. 提交信息

- 题号：1（素材审核与发布排期 Agent）
- GitHub：https://github.com/lagrange9430/agent-material-scheduler
- commit SHA：以仓库最新提交为准（把 SHA 回写进本文件会改变 SHA 本身，故不在此处固化）
- 评审入口建议顺序：本 README §4 → [docs/03-evaluation.md](docs/03-evaluation.md) → `examples/trace/`
