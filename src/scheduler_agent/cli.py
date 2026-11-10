"""CLI 入口：三选一里的 CLI 形态。

用法：
    python main.py --input examples/a1-normal.input.json
    python main.py --input examples/a3-info-gap.input.json --mode mock
    python main.py --check-model
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .agent import Agent, PolicyError, TaskInputError
from .config import REPO_ROOT, ConfigError, resolve_config
from .llm import LLMClient, LLMPolicy, check_model
from .policy import MockPolicy
from .store import DataStore, DraftStore
from .trace import write_trace

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_INCOMPLETE = 3
EXIT_FAILED = 4

STATUS_LABEL = {
    "completed": "已完成",
    "needs_confirmation": "待用户确认",
    "blocked": "全部阻断",
    "incomplete": "未完成（达到上限）",
    "failed": "失败",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="material-scheduler",
        description="素材审核与发布排期 Agent（只存草稿，不发布）",
    )
    parser.add_argument("--input", help="任务输入 JSON 路径")
    parser.add_argument("--mode", choices=["auto", "mock", "llm"], help="决策来源，默认读 AGENT_MODE")
    parser.add_argument("--out", help="输出 JSON 路径，默认 runtime/outputs/<task_id>.output.json")
    parser.add_argument("--trace", help="trace JSONL 路径，默认 runtime/trace/<task_id>.jsonl")
    parser.add_argument("--approve", action="append", default=[], metavar="M01=19:00",
                        help="模拟用户已确认的替代时段，可重复")
    parser.add_argument("--data-dir", help="数据目录，默认 ./data")
    parser.add_argument("--runtime-dir", help="运行时目录，默认 ./runtime")
    parser.add_argument("--json", action="store_true", help="只输出 JSON，不做中文渲染")
    parser.add_argument("--check-model", action="store_true", help="检测模型连通性后退出")
    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = resolve_config(
            mode_override=args.mode,
            data_dir=Path(args.data_dir) if args.data_dir else None,
            runtime_dir=Path(args.runtime_dir) if args.runtime_dir else None,
        )
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return EXIT_CONFIG

    if args.check_model:
        if not config.uses_llm:
            print("[连通性] 当前为 mock 模式，未配置 DEEPSEEK_API_KEY")
            return EXIT_OK
        client = LLMClient(config.api_key, config.base_url, config.model, config.timeout)
        result = check_model(client)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return EXIT_OK if result.get("ok") else EXIT_FAILED

    if not args.input:
        parser.error("--input 必填（除非使用 --check-model）")

    try:
        task = json.loads(Path(args.input).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[输入错误] 无法读取 {args.input}：{exc}", file=sys.stderr)
        return EXIT_FAILED

    if args.approve:
        task.setdefault("options", {})["approve"] = list(args.approve)

    if config.uses_llm:
        client = LLMClient(config.api_key, config.base_url, config.model, config.timeout)
        policy: Any = LLMPolicy(client)
    else:
        policy = MockPolicy()

    data = DataStore(config.data_dir)
    drafts = DraftStore(config.runtime_dir)
    agent = Agent(policy, data, drafts)

    task_id = str(task.get("task_id") or "task")
    try:
        result = agent.run(task)
        payload = result.payload
        rows = result.trace_rows
    except TaskInputError as exc:
        payload = _failure_payload(task_id, config, f"输入非法：{exc}")
        rows = []
        _emit(payload, config, args, rows)
        return EXIT_FAILED
    except PolicyError as exc:
        payload = _failure_payload(task_id, config, f"决策层不可用：{exc}")
        rows = []
        _emit(payload, config, args, rows)
        return EXIT_FAILED

    _emit(payload, config, args, rows)
    if payload["status"] == "incomplete":
        return EXIT_INCOMPLETE
    if payload["status"] == "failed":
        return EXIT_FAILED
    return EXIT_OK


def _failure_payload(task_id: str, config: Any, reason: str) -> Dict[str, Any]:
    return {
        "task_id": task_id,
        "status": "failed",
        "mode": f"llm:{config.model}" if config.uses_llm else "offline_mock",
        "steps_used": 0,
        "tool_calls_used": 0,
        "limits": {},
        "materials": [],
        "incomplete_reason": None,
        "trace_path": None,
        "error": reason,
    }


def _emit(payload: Dict[str, Any], config: Any, args: Any, rows: list) -> None:
    task_id = payload["task_id"]
    out_path = Path(args.out) if args.out else config.runtime_dir / "outputs" / f"{task_id}.output.json"
    trace_path = (
        Path(args.trace) if args.trace else config.runtime_dir / "trace" / f"{task_id}.jsonl"
    )

    write_trace(trace_path, rows, _summary(payload))
    payload["trace_path"] = _relative(trace_path)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.json:
        # stdout 保持纯 JSON，方便管道处理；路径信息走 stderr
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"输出：{_relative(out_path)}", file=sys.stderr)
        print(f"轨迹：{_relative(trace_path)}", file=sys.stderr)
    else:
        print(render(payload))
        print(f"\n输出：{_relative(out_path)}\n轨迹：{_relative(trace_path)}")


def _summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": payload["status"],
        "steps_used": payload["steps_used"],
        "tool_calls_used": payload["tool_calls_used"],
        "materials": [
            {
                "material_id": m["material_id"],
                "outcome": m["outcome"],
                "draft_status": m["draft_status"],
                "draft_id": m["draft_id"],
            }
            for m in payload.get("materials", [])
        ],
        "incomplete_reason": payload.get("incomplete_reason"),
    }


def _relative(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def render(payload: Dict[str, Any]) -> str:
    lines = []
    label = STATUS_LABEL.get(payload["status"], payload["status"])
    lines.append(f"任务 {payload['task_id']}｜状态 {label}（{payload['status']}）｜模式 {payload['mode']}")
    limits = payload.get("limits") or {}
    lines.append(
        "步数 {s}｜工具调用 {t}｜单素材上限 {ms}/{mt}".format(
            s=payload["steps_used"],
            t=payload["tool_calls_used"],
            ms=limits.get("max_steps_per_material", "-"),
            mt=limits.get("max_tool_calls_per_material", "-"),
        )
    )
    if payload.get("error"):
        lines.append(f"错误：{payload['error']}")
    for material in payload.get("materials", []):
        lines.append("")
        lines.append(
            f"[{material['material_id']}] 审核 {material['review_status']}"
            f"｜时段 {material['slot_status']}｜草稿 {material['draft_status']}"
        )
        for risk in material.get("risk_reasons", []):
            lines.append(
                f"    风险 {risk['rule_id']}（{risk['severity']}）：{risk['message']}"
                f"｜证据：{risk['evidence']}"
            )
        if material.get("suggested_slot"):
            line = f"    建议时段 {material['suggested_slot']}"
            if material.get("deviation"):
                line += (
                    f"（原 {material['deviation']['from']}，"
                    f"原因 {material['deviation']['reason']}）"
                )
            lines.append(line)
        if material.get("slot_evidence"):
            lines.append(f"    时段依据：{material['slot_evidence']}")
        for pending in material.get("pending_items", []):
            lines.append(f"    待确认：{pending}")
        if material.get("draft_id"):
            lines.append(f"    草稿：{material['draft_id']}")
        lines.append(f"    结论：{material['conclusion']}")
    if payload.get("incomplete_reason"):
        lines.append("")
        lines.append(f"未完成原因：{payload['incomplete_reason']}")
    lines.append("")
    lines.append("注意：本工具只保存草稿，不执行任何发布动作。")
    return "\n".join(lines)
