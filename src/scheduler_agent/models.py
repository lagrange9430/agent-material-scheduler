"""领域实体与状态机枚举。

原则：状态枚举是穷举的，不允许下游自由扩展字符串。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ReviewStatus(str, Enum):
    PASS = "pass"
    RISK = "risk"
    INFO_GAP = "info_gap"


class SlotStatus(str, Enum):
    AVAILABLE = "available"
    CONFLICT_RESOLVED = "conflict_resolved"
    CONFLICT_UNRESOLVED = "conflict_unresolved"
    NOT_CHECKED = "not_checked"


class DraftStatus(str, Enum):
    SAVED = "saved"
    DUPLICATE = "duplicate"
    UPDATED = "updated"
    FAILED = "failed"
    NOT_SAVED = "not_saved"


class TaskStatus(str, Enum):
    COMPLETED = "completed"
    NEEDS_CONFIRMATION = "needs_confirmation"
    BLOCKED = "blocked"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


# 动作白名单：模型只能从这里选，工具执行器也只认这四个工具动作。
TOOL_ACTIONS = frozenset({"read_material", "check_rules", "query_slots", "save_draft"})
TERMINAL_ACTIONS = frozenset({"request_confirmation", "block", "finish_material"})
ALL_ACTIONS = TOOL_ACTIONS | TERMINAL_ACTIONS


@dataclass(frozen=True)
class Action:
    """模型（或 mock 策略）选择的一个动作。"""

    name: str
    args: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"action": self.name, "args": dict(self.args), "reason": self.reason}

    @property
    def is_tool(self) -> bool:
        return self.name in TOOL_ACTIONS


@dataclass
class Observation:
    """一次工具执行的结果。ok=False 也必须有 error_code 与 message。"""

    action: str
    ok: bool
    data: Dict[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None
    message: str = ""
    summary: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "ok": self.ok,
            "error_code": self.error_code,
            "message": self.message,
            "summary": self.summary,
        }


@dataclass
class MaterialState:
    """单个素材的决策上下文：既存观测，也存从观测里解析出的结构化事实。"""

    spec: Dict[str, Any]
    material_id: str = ""
    observations: List[Dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    tool_calls: int = 0
    finished: bool = False
    terminal: Optional[Action] = None

    material: Optional[Dict[str, Any]] = None
    missing_fields: List[str] = field(default_factory=list)
    read_error: Optional[str] = None
    rules_checked: bool = False
    violations: List[Dict[str, Any]] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    slots: Optional[Dict[str, Any]] = None
    chosen_slot: Optional[str] = None
    slot_status: SlotStatus = SlotStatus.NOT_CHECKED
    slot_evidence: str = ""
    draft: Optional[Dict[str, Any]] = None
    draft_status: DraftStatus = DraftStatus.NOT_SAVED
    needs_confirmation: bool = False
    pending: List[str] = field(default_factory=list)
    review_status: ReviewStatus = ReviewStatus.PASS
    repairs: int = 0
    outcome: str = ""
    approved_slot: Optional[str] = None

    def __post_init__(self) -> None:
        self.material_id = str(self.spec.get("material_id", ""))

    # ---- 观测检索 -------------------------------------------------
    def has(self, action_name: str) -> bool:
        return any(o["action"] == action_name for o in self.observations)

    def last(self, action_name: str) -> Optional[Dict[str, Any]]:
        for o in reversed(self.observations):
            if o["action"] == action_name:
                return o
        return None

    # ---- 观测解析 -------------------------------------------------
    def observe(self, action_name: str, obs: Observation) -> None:
        self.observations.append(
            {
                "action": action_name,
                "ok": obs.ok,
                "error_code": obs.error_code,
                "summary": obs.summary,
                "data": obs.data,
            }
        )
        if action_name == "read_material":
            # MISSING_FIELDS 虽为 ok=False，但仍带回部分元数据，必须保留
            if obs.ok or obs.error_code == "MISSING_FIELDS":
                self.material = obs.data.get("material")
                self.missing_fields = list(obs.data.get("missing_fields") or [])
            else:
                self.read_error = obs.error_code
        elif action_name == "check_rules":
            if obs.ok:
                self.rules_checked = True
                self.violations = list(obs.data.get("violations") or [])
                self.skipped = list(obs.data.get("skipped") or [])
            else:
                self.read_error = obs.error_code
        elif action_name == "query_slots":
            if obs.ok:
                self.slots = obs.data
        elif action_name == "save_draft":
            self.draft = obs.data
            if obs.ok:
                if obs.data.get("updated"):
                    self.draft_status = DraftStatus.UPDATED
                elif obs.data.get("created") is False:
                    self.draft_status = DraftStatus.DUPLICATE
                else:
                    self.draft_status = DraftStatus.SAVED
            else:
                self.draft_status = DraftStatus.FAILED

    # ---- 派生事实 -------------------------------------------------
    @property
    def blocking_violations(self) -> List[Dict[str, Any]]:
        return [v for v in self.violations if v.get("blocks_schedule")]

    @property
    def material_ok(self) -> bool:
        """是否拿到了可用于继续判断的元数据（完整或不完整）。"""
        return self.material is not None
