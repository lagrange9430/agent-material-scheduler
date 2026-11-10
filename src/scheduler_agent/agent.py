"""ReAct 主循环与结果组装。

循环里只有三件事：选动作 → 执行工具 → 解析观测。
「怎么选」在 policy 层，「执行出什么」在 tools 层，本层只负责预算、收敛和落账。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    ALL_ACTIONS,
    Action,
    DraftStatus,
    MaterialState,
    Observation,
    ReviewStatus,
    SlotStatus,
    TaskStatus,
)
from .store import DataStore, DraftStore
from .tools import ToolRegistry

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SLOT_RE = re.compile(r"^([01]\d|2[0-3]):00$")


class TaskInputError(RuntimeError):
    """输入非法：直接 failed，不进入决策循环。"""


class PolicyError(RuntimeError):
    """决策层不可用（模型超时/输出不可解析）：任务 failed。"""


def _minutes(slot: str) -> int:
    hour, minute = slot.split(":")
    return int(hour) * 60 + int(minute)


def _clamp(value: Any, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = low
    return max(low, min(high, number))


def _parse_approve(items: List[str]) -> Dict[str, str]:
    approved: Dict[str, str] = {}
    for item in items:
        if not isinstance(item, str) or "=" not in item:
            continue
        material_id, slot = item.split("=", 1)
        approved[material_id.strip()] = slot.strip()
    return approved


@dataclass
class AgentResult:
    payload: Dict[str, Any]
    trace_rows: List[Dict[str, Any]]


class Agent:
    def __init__(
        self,
        policy: Any,
        data: DataStore,
        drafts: DraftStore,
        faults: Optional[List[str]] = None,
        max_total_tool_calls: int = 40,
    ) -> None:
        self.policy = policy
        self.data = data
        self.drafts = drafts
        self.base_faults = list(faults or [])
        self.max_total_tool_calls = max_total_tool_calls
        self.tools: Optional[ToolRegistry] = None
        self._total_tools = 0
        self._rows: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    def run(self, task: Dict[str, Any]) -> AgentResult:
        self._validate(task)
        task_id = str(task.get("task_id") or "task")
        date = task["date"]
        options = task.get("options") or {}
        max_steps = _clamp(options.get("max_steps", 8), 1, 20)
        max_tools = _clamp(options.get("max_tool_calls", 6), 1, 20)
        approve = _parse_approve(list(options.get("approve") or []))

        faults = set(self.base_faults) | set(task.get("faults") or [])
        self.tools = ToolRegistry(self.data, self.drafts, faults)
        self._total_tools = 0
        self._rows = []

        states: List[MaterialState] = []
        incomplete_reason: Optional[str] = None
        total_steps = 0

        for raw_spec in task["materials"]:
            spec = dict(raw_spec)
            spec["date"] = date
            spec["task_id"] = task_id
            spec["schedule_file"] = str(spec.get("schedule_file") or options.get("schedule_file") or "schedule.json")

            state = MaterialState(spec)
            state.approved_slot = approve.get(state.material_id)

            reason = self._run_one(state, max_steps, max_tools)
            states.append(state)
            total_steps += state.steps

            if reason and incomplete_reason is None:
                incomplete_reason = reason
            if reason:
                # 预算已耗尽，不再开启新素材（避免产生没有结论的条目）
                break

        results = [self._material_result(state) for state in states]
        status = self._task_status(states, incomplete_reason)

        payload: Dict[str, Any] = {
            "task_id": task_id,
            "status": status.value,
            "mode": getattr(self.policy, "mode", "unknown"),
            "steps_used": total_steps,
            "tool_calls_used": self._total_tools,
            "limits": {
                "max_steps_per_material": max_steps,
                "max_tool_calls_per_material": max_tools,
                "max_total_tool_calls": self.max_total_tool_calls,
            },
            "materials": results,
            "incomplete_reason": incomplete_reason,
            "trace_path": None,
        }
        return AgentResult(payload=payload, trace_rows=self._rows)

    # ------------------------------------------------------------------
    def _validate(self, task: Dict[str, Any]) -> None:
        if not isinstance(task, dict):
            raise TaskInputError("输入必须是 JSON 对象")
        date = task.get("date")
        if not isinstance(date, str) or not DATE_RE.match(date):
            raise TaskInputError(f"date 非法：{date!r}，应为 YYYY-MM-DD")
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError as exc:
            raise TaskInputError(f"date 不存在：{date}") from exc

        materials = task.get("materials")
        if not isinstance(materials, list) or not materials:
            raise TaskInputError("materials 必须是非空数组")

        for spec in materials:
            if not isinstance(spec, dict) or not spec.get("material_id"):
                raise TaskInputError("每个素材都必须有 material_id")
            requested = spec.get("requested_time")
            constraint = str(spec.get("time_constraint") or "preferred").lower()
            if constraint not in ("hard", "preferred"):
                raise TaskInputError(f"time_constraint 非法：{constraint}，只能是 hard 或 preferred")
            if requested is not None:
                if not isinstance(requested, str) or not SLOT_RE.match(requested):
                    raise TaskInputError(f"requested_time 非法：{requested!r}，应形如 18:00")
                if not (540 <= _minutes(requested) <= 1260):
                    raise TaskInputError(f"requested_time 超出服务时段 09:00~21:00：{requested}")
            if constraint == "hard" and requested is None:
                raise TaskInputError("time_constraint=hard 时必须提供 requested_time")

    # ------------------------------------------------------------------
    def _run_one(self, state: MaterialState, max_steps: int, max_tools: int) -> Optional[str]:
        reason: Optional[str] = None
        while state.steps < max_steps:
            action = self._guard(state, self._choose(state))

            if action.is_tool:
                if state.tool_calls >= max_tools:
                    reason = (
                        f"tool_budget: 素材 {state.material_id} 工具调用达到上限 "
                        f"{max_tools}，未能给出最终结论"
                    )
                    break
                if self._total_tools >= self.max_total_tool_calls:
                    reason = f"task_tool_budget: 任务工具调用总量达到上限 {self.max_total_tool_calls}"
                    break

                state.steps += 1
                observation = self.tools.call(action, state)  # type: ignore[union-attr]
                state.tool_calls += 1
                self._total_tools += 1
                state.observe(action.name, observation)
                self._rows.append(self._row(state, action, observation))
                self._apply_decision_side_effects(state, action, observation)
            else:
                state.steps += 1
                state.terminal = action
                state.finished = True
                self._apply_decision_side_effects(state, action, None)
                self._rows.append(self._row(state, action, None))
                break
        else:
            reason = f"step_budget: 素材 {state.material_id} 步数达到上限 {max_steps}，未能给出最终结论"

        self._finalize(state, reason)
        return reason

    def _guard(self, state: MaterialState, action: Action) -> Action:
        """硬约束护栏：即使决策层出错，也不能悄悄改掉用户声明的发布时间。"""
        if action.name == "save_draft":
            requested = state.spec.get("requested_time")
            constraint = str(state.spec.get("time_constraint") or "preferred").lower()
            slot = str(action.args.get("slot") or "")
            if constraint == "hard" and requested and slot and slot != requested:
                return Action(
                    "block",
                    {
                        "reason": f"决策层尝试把硬约束时段 {requested} 改为 {slot}，已被护栏拦截",
                        "slot_status": "conflict_unresolved",
                    },
                    "硬约束护栏：用户声明不可改期，拒绝写入替代时段",
                )
        return action

    def _choose(self, state: MaterialState) -> Action:
        try:
            action = self.policy.next(state)
        except PolicyError:
            raise
        except Exception as exc:  # noqa: BLE001 - 决策层任何异常都转成明确失败
            raise PolicyError(f"决策失败：{exc}") from exc
        if not isinstance(action, Action):
            raise PolicyError(f"决策层返回了非 Action 对象：{type(action)!r}")
        if action.name not in ALL_ACTIONS:
            raise PolicyError(f"决策层选择了非法动作：{action.name}")
        return action

    def _row(
        self, state: MaterialState, action: Action, observation: Optional[Observation]
    ) -> Dict[str, Any]:
        return {
            "step": len(self._rows) + 1,
            "material_id": state.material_id,
            "action": action.name,
            "args": action.args,
            "reason": action.reason,
            "ok": True if observation is None else observation.ok,
            "error_code": None if observation is None else observation.error_code,
            "result_summary": "（终止动作，无工具返回）" if observation is None else observation.summary,
        }

    def _apply_decision_side_effects(
        self, state: MaterialState, action: Action, observation: Optional[Observation]
    ) -> None:
        """把动作意图与观测结果落到状态上（决策已发生，此处只记账）。"""
        if action.name == "save_draft":
            slot = str(action.args.get("slot") or "")
            state.chosen_slot = slot or None
            # 幂等命中时以草稿记录里的待确认状态为准，避免第二次运行把"待确认"洗掉
            from_observation = observation.data.get("needs_confirmation") if observation else None
            state.needs_confirmation = (
                bool(from_observation)
                if from_observation is not None
                else bool(action.args.get("needs_confirmation"))
            )
            requested = state.spec.get("requested_time")
            if slot and slot == requested:
                state.slot_status = SlotStatus.AVAILABLE
            elif slot:
                state.slot_status = SlotStatus.CONFLICT_RESOLVED
            else:
                state.slot_status = SlotStatus.CONFLICT_UNRESOLVED
        elif action.name in ("block", "request_confirmation"):
            raw_status = action.args.get("slot_status")
            if raw_status:
                try:
                    state.slot_status = SlotStatus(raw_status)
                except ValueError:
                    state.slot_status = SlotStatus.NOT_CHECKED
        if action.name == "query_slots" and observation is not None and observation.ok:
            state.slot_evidence = observation.summary

    # ------------------------------------------------------------------
    def _finalize(self, state: MaterialState, incomplete_reason: Optional[str]) -> None:
        requested = state.spec.get("requested_time")
        terminal_name = state.terminal.name if state.terminal else None

        if incomplete_reason:
            state.outcome = "incomplete"
        elif state.draft_status is DraftStatus.FAILED:
            state.outcome = "save_failed"
        elif terminal_name == "block":
            state.outcome = "blocked"
        elif terminal_name == "request_confirmation":
            state.outcome = "pending_confirmation"
            state.needs_confirmation = True
        elif terminal_name == "finish_material":
            code = str((state.terminal.args or {}).get("outcome") or "pass")
            if code == "info_gap":
                state.outcome = "info_gap"
            elif code == "approved":
                state.outcome = "approved"
            elif state.draft_status is DraftStatus.DUPLICATE:
                state.outcome = "duplicate"
            elif state.draft_status is DraftStatus.SAVED:
                state.outcome = "scheduled"
            else:
                state.outcome = "not_scheduled"
        else:
            state.outcome = "incomplete"

        # 审阅结论：风险优先，其次信息不足
        if state.blocking_violations:
            state.review_status = ReviewStatus.RISK
        elif state.skipped or state.missing_fields:
            state.review_status = ReviewStatus.INFO_GAP
        else:
            state.review_status = ReviewStatus.PASS

        # 阻断原因可由观测事实推出时，补齐时段状态，不依赖决策层的自述
        requested = state.spec.get("requested_time")
        if state.outcome == "blocked" and state.slot_status is SlotStatus.NOT_CHECKED and state.slots:
            occupied = {row["slot"] for row in state.slots.get("occupied", [])}
            if requested and requested in occupied:
                state.slot_status = SlotStatus.CONFLICT_UNRESOLVED

        if state.needs_confirmation:
            if state.chosen_slot and requested and state.chosen_slot != requested:
                state.pending.append(
                    f"确认把 {requested} 调整为 {state.chosen_slot}（原时段已被占用）"
                )
            elif state.terminal and state.terminal.args.get("reason"):
                state.pending.append(str(state.terminal.args["reason"]))
            elif not state.pending:
                state.pending.append("需要用户确认后才能继续")

    def _task_status(
        self, states: List[MaterialState], incomplete_reason: Optional[str]
    ) -> TaskStatus:
        if incomplete_reason:
            return TaskStatus.INCOMPLETE
        outcomes = [s.outcome for s in states]
        if any(o == "save_failed" for o in outcomes):
            return TaskStatus.FAILED
        if any(s.needs_confirmation for s in states):
            return TaskStatus.NEEDS_CONFIRMATION
        if outcomes and all(o == "blocked" for o in outcomes):
            return TaskStatus.BLOCKED
        return TaskStatus.COMPLETED

    # ------------------------------------------------------------------
    def _material_result(self, state: MaterialState) -> Dict[str, Any]:
        date = str(state.spec.get("date"))
        requested = state.spec.get("requested_time")
        chosen = state.chosen_slot

        deviation = None
        if requested and chosen and requested != chosen:
            deviation = {
                "from": requested,
                "to": chosen,
                "reason": "requested slot occupied",
            }

        draft_id = None
        if state.draft_status in (DraftStatus.SAVED, DraftStatus.DUPLICATE, DraftStatus.UPDATED) and state.draft:
            draft_id = state.draft.get("draft_id")

        return {
            "material_id": state.material_id,
            "review_status": state.review_status.value,
            "risk_reasons": [
                {
                    "rule_id": v["rule_id"],
                    "severity": v["severity"],
                    "message": v["message"],
                    "evidence": v["evidence"],
                }
                for v in state.violations
            ],
            "requested_slot": f"{date}T{requested}" if requested else None,
            "suggested_slot": f"{date}T{chosen}" if chosen else None,
            "slot_status": state.slot_status.value,
            "slot_evidence": state.slot_evidence,
            "needs_user_confirmation": state.needs_confirmation,
            "pending_items": state.pending,
            "draft_status": state.draft_status.value,
            "draft_id": draft_id,
            "deviation": deviation,
            "outcome": state.outcome,
            "conclusion": self._conclusion(state),
            "steps_used": state.steps,
            "tool_calls_used": state.tool_calls,
        }

    def _conclusion(self, state: MaterialState) -> str:
        draft_id = (state.draft or {}).get("draft_id")
        outcome = state.outcome
        if outcome == "scheduled":
            return f"已存草稿 {draft_id}（{state.chosen_slot}），未发布"
        if outcome == "duplicate":
            return f"草稿已存在 {draft_id}，未重复创建，未发布"
        if outcome == "approved":
            return f"已按用户确认将草稿 {draft_id} 更新为 {state.chosen_slot}，未发布"
        if outcome == "pending_confirmation":
            return "已给出建议时段，等待用户确认后才能定稿，未发布"
        if outcome == "blocked":
            # 结论只由观测事实拼装，不直接引用决策层的自由文本，避免把幻觉写进结论
            if state.blocking_violations:
                ids = ", ".join(sorted({v["rule_id"] for v in state.blocking_violations}))
                return f"已阻断：命中规则 {ids}，不进入排期"
            if state.read_error:
                return f"已阻断：读取失败（{state.read_error}）"
            if state.slot_status is SlotStatus.CONFLICT_UNRESOLVED:
                requested_slot = state.spec.get("requested_time")
                return f"已阻断：{requested_slot} 已被占用且不可改期，未擅自调整"
            return "已阻断：存在阻断项，详见 trace"
        if outcome == "info_gap":
            fields = ", ".join(state.missing_fields) or "关键字段"
            return f"资料不足，无法判定：{fields}，需补充后再提交"
        if outcome == "save_failed":
            return "草稿保存失败，未产生草稿"
        if outcome == "incomplete":
            return "达到执行上限，未完成"
        return "未完成"
