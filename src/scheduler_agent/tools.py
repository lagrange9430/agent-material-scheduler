"""工具层：4 个工具，唯一写操作是 save_draft。

设计约束：
* 每个工具都必须显式表达失败（ok=False + error_code + message），不允许静默降级。
* 工具失败不重试；是否重试属于上层的决策，不属于工具。
* 不存在 publish / 分发 类工具。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from .models import Action, MaterialState, Observation
from .store import DataStore, DraftStore, draft_id_for, idempotency_key

SLOT_RE = re.compile(r"^([01]\d|2[0-3]):00$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

FALLBACK_SLOTS: List[str] = []


def _minutes(slot: str) -> int:
    hour, minute = slot.split(":")
    return int(hour) * 60 + int(minute)


def _dig(payload: Optional[Dict[str, Any]], dotted: str) -> Any:
    node: Any = payload
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _fmt_violation(rule: Dict[str, Any], evidence: str) -> Dict[str, Any]:
    return {
        "rule_id": rule["id"],
        "rule_name": rule["name"],
        "severity": rule.get("severity", "major"),
        "blocks_schedule": bool(rule.get("blocks_schedule", True)),
        "message": rule.get("message", ""),
        "evidence": evidence,
    }


class ToolRegistry:
    def __init__(
        self,
        data: DataStore,
        drafts: DraftStore,
        faults: Optional[Set[str]] = None,
    ) -> None:
        self.data = data
        self.drafts = drafts
        self.faults = set(faults or [])

    # ------------------------------------------------------------------
    def call(self, action: Action, state: MaterialState) -> Observation:
        if not action.is_tool:
            return Observation(
                action=action.name,
                ok=False,
                error_code="NOT_A_TOOL",
                message=f"{action.name} 不是工具动作",
                summary=f"{action.name} 不是工具动作",
            )
        fault_key = f"{state.material_id}:{action.name}"
        if fault_key in self.faults:
            return Observation(
                action=action.name,
                ok=False,
                error_code="SAVE_FAILED" if action.name == "save_draft" else "INJECTED_FAULT",
                message=f"注入故障 {fault_key}：工具不可用。下一步应变更为上报失败或改用其它路径，而不是重复调用。",
                summary=f"注入故障：{fault_key}",
            )
        handler = getattr(self, f"_tool_{action.name}", None)
        if handler is None:
            return Observation(
                action=action.name,
                ok=False,
                error_code="UNKNOWN_TOOL",
                message=f"未实现的工具：{action.name}",
                summary=f"未实现的工具：{action.name}",
            )
        return handler(action.args or {}, state)

    # ---- read_material -------------------------------------------------
    def _tool_read_material(self, args: Dict[str, Any], state: MaterialState) -> Observation:
        material_id = str(args.get("material_id") or state.material_id)
        material = self.data.material(material_id)
        if material is None:
            return Observation(
                action="read_material",
                ok=False,
                error_code="NOT_FOUND",
                message=f"素材 {material_id} 不存在于素材库；无法审核，也无法排期。",
                summary=f"读取失败：NOT_FOUND（{material_id}）",
            )
        missing = self._missing_fields(material)
        if missing:
            return Observation(
                action="read_material",
                ok=False,
                error_code="MISSING_FIELDS",
                data={"material": material, "missing_fields": missing},
                message=f"素材 {material_id} 缺少字段：{', '.join(missing)}；可继续做部分规则检查，但不能给出可发布结论。",
                summary=f"读取部分成功：{material_id}，缺失字段 {', '.join(missing)}",
            )
        return Observation(
            action="read_material",
            ok=True,
            data={"material": material, "missing_fields": []},
            summary=(
                f"读取成功：{material_id}《{material.get('title')}》，"
                f"时长 {material.get('duration_seconds')}s，类目 {material.get('category')}"
            ),
        )

    def _missing_fields(self, material: Dict[str, Any]) -> List[str]:
        required = [
            "title",
            "duration_seconds",
            "category",
            "platform",
            "copyright.status",
            "copyright.scope",
            "copyright.expires",
        ]
        missing: List[str] = []
        for field in required:
            if _dig(material, field) in (None, "", []):
                missing.append(field)
        if material.get("has_ad_label") is None:
            missing.append("has_ad_label")
        return missing

    # ---- check_rules ---------------------------------------------------
    def _tool_check_rules(self, args: Dict[str, Any], state: MaterialState) -> Observation:
        if not self.data.rules:
            return Observation(
                action="check_rules",
                ok=False,
                error_code="RULEBOOK_UNAVAILABLE",
                message="规则库不可读，无法完成合规检查。",
                summary="规则检查失败：RULEBOOK_UNAVAILABLE",
            )
        material = state.material or self.data.material(str(args.get("material_id") or state.material_id))
        if material is None:
            return Observation(
                action="check_rules",
                ok=False,
                error_code="NO_MATERIAL",
                message="尚未成功读取素材，无法做规则检查。",
                summary="规则检查失败：NO_MATERIAL",
            )

        violations: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        for rule in self.data.rules:
            verdict, evidence, skip_reason = self._evaluate_rule(rule, material, state.spec)
            if skip_reason:
                skipped.append({"rule_id": rule["id"], "rule_name": rule["name"], "reason": skip_reason})
            elif verdict is False:
                violations.append(_fmt_violation(rule, evidence))

        summary = "规则检查："
        if violations:
            summary += "；".join(f"{v['rule_id']} {v['rule_name']}({v['severity']})" for v in violations)
        else:
            summary += "无违规"
        if skipped:
            summary += "；跳过 " + ", ".join(f"{s['rule_id']}({s['reason']})" for s in skipped)
        return Observation(
            action="check_rules",
            ok=True,
            data={"violations": violations, "skipped": skipped, "checked": len(self.data.rules)},
            summary=summary,
        )

    def _evaluate_rule(
        self, rule: Dict[str, Any], material: Dict[str, Any], spec: Dict[str, Any]
    ) -> tuple[Optional[bool], str, Optional[str]]:
        """返回 (是否通过, 证据, 跳过原因)。"""
        check = rule.get("check")

        if check == "copyright":
            status = _dig(material, "copyright.status")
            scope = _dig(material, "copyright.scope")
            expires = _dig(material, "copyright.expires")
            if status is None:
                return None, "", "missing_field:copyright.status"
            if scope is None:
                return None, "", "missing_field:copyright.scope"
            if expires is None:
                return None, "", "missing_field:copyright.expires"
            problems = []
            if status != "granted":
                problems.append(f"status={status}")
            platform = material.get("platform")
            if platform not in scope:
                problems.append(f"platform={platform} 不在授权范围 {scope}")
            if expires < str(spec.get("date", "")):
                problems.append(f"expires={expires} 已过期")
            if problems:
                return False, "；".join(problems), None
            return True, f"status=granted, scope={scope}, expires={expires}", None

        if check == "forbidden_keywords":
            texts = []
            for field in rule.get("fields", []):
                value = _dig(material, field)
                if value is None:
                    return None, "", f"missing_field:{field}"
                texts.append(str(value))
            haystack = " ".join(texts)
            hits = [kw for kw in rule.get("keywords", []) if kw in haystack]
            if hits:
                return False, f"命中禁用词 {hits}；原文：{haystack}", None
            return True, f"未命中禁用词；原文：{haystack}", None

        if check == "max_int":
            value = _dig(material, rule["field"])
            if value is None:
                return None, "", f"missing_field:{rule['field']}"
            if value > int(rule["limit"]):
                return False, f"{rule['field']}={value} > 上限 {rule['limit']}", None
            return True, f"{rule['field']}={value} <= {rule['limit']}", None

        if check == "not_scheduled":
            booked = self.data.booked_materials(str(spec.get("schedule_file") or "schedule.json"))
            if material.get("material_id") in booked:
                return False, f"素材 {material['material_id']} 已出现在当天排期表", None
            return True, "当天排期表中无该素材", None

        if check == "conditional_flag":
            category = _dig(material, rule["when_field"])
            if category is None:
                return None, "", f"missing_field:{rule['when_field']}"
            if category not in rule.get("when_in", []):
                return True, f"类目 {category} 不适用", None
            flag = material.get(rule["require_field"])
            if flag is None:
                return None, "", f"missing_field:{rule['require_field']}"
            if flag is not True:
                return False, f"类目 {category} 但 {rule['require_field']}={flag}", None
            return True, f"类目 {category} 且已标注", None

        return None, "", f"unknown_check:{check}"

    # ---- query_slots ---------------------------------------------------
    def _tool_query_slots(self, args: Dict[str, Any], state: MaterialState) -> Observation:
        date = str(args.get("date") or state.spec.get("date") or "")
        if not DATE_RE.match(date):
            return Observation(
                action="query_slots",
                ok=False,
                error_code="INVALID_DATE",
                message=f"日期格式非法：{date!r}，应为 YYYY-MM-DD。",
                summary=f"查时段失败：INVALID_DATE（{date}）",
            )
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            return Observation(
                action="query_slots",
                ok=False,
                error_code="INVALID_DATE",
                message=f"日期不存在：{date}。",
                summary=f"查时段失败：INVALID_DATE（{date}）",
            )

        schedule_file = str(state.spec.get("schedule_file") or "schedule.json")
        booked = self.data.booked_slots(schedule_file)
        duration_min = max(1, int(round(float(args.get("duration_min") or 1))))
        day_start = _minutes(self.data.schedule(schedule_file).get("day_start", "09:00"))
        day_end = _minutes(self.data.schedule(schedule_file).get("day_end", "22:00"))

        available: List[str] = []
        for start in range(day_start, day_end, 60):
            slot = f"{start // 60:02d}:00"
            if not SLOT_RE.match(slot):
                continue
            if slot in booked:
                continue
            if start + duration_min > day_end:
                continue
            available.append(slot)
        available.sort(key=_minutes)

        return Observation(
            action="query_slots",
            ok=True,
            data={
                "date": date,
                "duration_min": duration_min,
                "occupied": [{"slot": s, "material_id": m} for s, m in sorted(booked.items())],
                "available": available,
            },
            summary=(
                f"{date} 占用 "
                + (", ".join(f"{s}({m})" for s, m in sorted(booked.items())) or "无")
                + f"；可用 {', '.join(available) or '无'}"
            ),
        )

    # ---- save_draft ----------------------------------------------------
    def _tool_save_draft(self, args: Dict[str, Any], state: MaterialState) -> Observation:
        slot = str(args.get("slot") or "")
        date = str(args.get("date") or state.spec.get("date") or "")
        if not SLOT_RE.match(slot):
            return Observation(
                action="save_draft",
                ok=False,
                error_code="INVALID_SLOT",
                message=f"时段格式非法：{slot!r}，应形如 19:00。",
                summary=f"保存失败：INVALID_SLOT（{slot}）",
            )
        schedule_file = str(state.spec.get("schedule_file") or "schedule.json")
        occupied = self.data.booked_slots(schedule_file)
        if slot in occupied:
            return Observation(
                action="save_draft",
                ok=False,
                error_code="CONFLICT_SLOT",
                message=f"{slot} 已被 {occupied[slot]} 占用，拒绝写入草稿。",
                summary=f"保存失败：CONFLICT_SLOT（{slot} 被 {occupied[slot]} 占用）",
            )

        key = idempotency_key(state.material_id, date, slot)
        existing = self.drafts.find(key)
        wants_confirmation = bool(args.get("needs_confirmation"))
        if existing is not None:
            # 幂等：请求完全一致 → 不新建、不改写
            if bool(existing.get("needs_confirmation")) == wants_confirmation:
                return Observation(
                    action="save_draft",
                    ok=True,
                    data={
                        "draft_id": existing["draft_id"],
                        "revision": existing["revision"],
                        "created": False,
                        "slot": slot,
                        "needs_confirmation": bool(existing.get("needs_confirmation")),
                    },
                    summary=f"幂等命中：草稿 {existing['draft_id']} 已存在，本次未新建",
                )
            # 只允许 pending → confirmed（用户确认），不允许把已确认的草稿降级回待确认
            if wants_confirmation:
                return Observation(
                    action="save_draft",
                    ok=True,
                    data={
                        "draft_id": existing["draft_id"],
                        "revision": existing["revision"],
                        "created": False,
                        "slot": slot,
                        "needs_confirmation": False,
                    },
                    summary=(
                        f"草稿 {existing['draft_id']} 已确认，重复请求不会把它改回待确认"
                    ),
                )
            updated = dict(existing)
            updated["needs_confirmation"] = False
            updated["status"] = "draft_confirmed"
            updated["revision"] = int(existing.get("revision", 1)) + 1
            updated["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            self.drafts.put(key, updated)
            return Observation(
                action="save_draft",
                ok=True,
                data={
                    "draft_id": updated["draft_id"],
                    "revision": updated["revision"],
                    "created": False,
                    "updated": True,
                    "slot": slot,
                    "needs_confirmation": False,
                },
                summary=(
                    f"用户已确认：草稿 {updated['draft_id']} 就地更新为 confirmed"
                    f"（revision {updated['revision']}），未新建第二份"
                ),
            )

        record = {
            "draft_id": draft_id_for(key),
            "key": key,
            "task_id": str(args.get("task_id") or state.spec.get("task_id") or ""),
            "material_id": state.material_id,
            "date": date,
            "proposed_slot": slot,
            "requested_slot": state.spec.get("requested_time"),
            "needs_confirmation": bool(args.get("needs_confirmation")),
            "status": "pending_confirmation" if args.get("needs_confirmation") else "draft_saved",
            "revision": 1,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        self.drafts.put(key, record)
        return Observation(
            action="save_draft",
            ok=True,
            data={
                "draft_id": record["draft_id"],
                "revision": 1,
                "created": True,
                "slot": slot,
                "needs_confirmation": record["needs_confirmation"],
            },
            summary=f"已保存草稿 {record['draft_id']}（{slot}，状态 {record['status']}）",
        )
