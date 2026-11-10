"""离线 mock 策略：把「观测」映射为「下一个动作」。

它不是执行脚本：每一行判断读的都是上一次工具的真实返回
（NOT_FOUND / MISSING_FIELDS / violations / skipped / available ...），
所以同一素材在不同数据下会走出不同的动作序列（见 docs/02 §6）。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from .models import Action, MaterialState, SlotStatus


def _minutes(slot: str) -> int:
    hour, minute = slot.split(":")
    return int(hour) * 60 + int(minute)


def pick_alternative(requested: str, available: List[str]) -> Optional[str]:
    """优先取晚于原时段的最近整点，其次取早于原时段的最近整点。"""
    later = [s for s in available if _minutes(s) > _minutes(requested)]
    earlier = [s for s in available if _minutes(s) < _minutes(requested)]
    if later:
        return later[0]
    if earlier:
        return earlier[-1]
    return None


class MockPolicy:
    mode = "offline_mock"

    def next(self, state: MaterialState) -> Action:
        mid = state.material_id

        # 1. 无观测：必须先读素材
        if not state.has("read_material"):
            return Action(
                "read_material",
                {"material_id": mid},
                "尚无线索：任何审核或排期判断都需要先拿到素材元数据",
            )

        # 2. 读取失败分支
        if state.read_error == "NOT_FOUND":
            return Action(
                "block",
                {"reason": f"素材 {mid} 不存在（NOT_FOUND）", "slot_status": "not_checked"},
                "读取返回 NOT_FOUND：没有素材就没有排期对象，不应继续占用时段",
            )
        if not state.material_ok:
            return Action(
                "block",
                {"reason": f"素材 {mid} 读取失败：{state.read_error}", "slot_status": "not_checked"},
                "读取未拿到可用元数据，继续推进只会基于猜测",
            )

        # 3. 规则检查：即使字段不全也要跑，用来区分「可判定」与「不可判定」
        if not state.has("check_rules"):
            if state.missing_fields:
                reason = (
                    f"素材缺少 {len(state.missing_fields)} 个字段，先执行规则检查看哪些规则仍可判定，"
                    "避免把“部分可知”误报成“全部未知”"
                )
            else:
                reason = "元数据完整，进入合规检查，合规通过前不占用时段"
            return Action("check_rules", {"material_id": mid}, reason)

        checked = state.last("check_rules") or {}
        if not checked.get("ok"):
            return Action(
                "block",
                {"reason": f"规则检查不可用：{checked.get('error_code')}", "slot_status": "not_checked"},
                "规则库不可用意味着无法确认合规，不能冒险排期",
            )

        # 4. 有阻断违规：停止排期
        if state.blocking_violations:
            ids = ", ".join(sorted({v["rule_id"] for v in state.blocking_violations}))
            return Action(
                "block",
                {"reason": f"命中阻断规则：{ids}", "slot_status": "not_checked"},
                f"规则检查返回 {len(state.blocking_violations)} 条阻断违规，合规不可绕过",
            )

        # 5. 有规则被跳过（缺字段）：不能把「没查出问题」当成「没问题」
        if state.skipped:
            ids = ", ".join(sorted({s["rule_id"] for s in state.skipped}))
            return Action(
                "finish_material",
                {"outcome": "info_gap"},
                f"规则 {ids} 因缺字段被跳过，现有信息不足以判定可发布，直接排期等于用未知冒充通过",
            )

        # 6. 查时段
        if not state.has("query_slots"):
            duration = state.material.get("duration_seconds") or 60 if state.material else 60
            return Action(
                "query_slots",
                {"date": state.spec.get("date"), "duration_min": max(1, math.ceil(duration / 60))},
                "合规已确认，检查期望时段是否可用，再决定保存还是找替代",
            )

        # 7. 已经保存过草稿：根据保存结果收尾
        if state.has("save_draft"):
            saved = state.last("save_draft") or {}
            data = saved.get("data") or {}
            if not saved.get("ok"):
                return Action(
                    "finish_material",
                    {"outcome": "save_failed"},
                    "保存返回失败；重试属于固定流程而非决策，应当如实上报失败",
                )
            if data.get("updated"):
                return Action(
                    "finish_material",
                    {"outcome": "approved"},
                    "用户已确认替代时段，草稿就地更新为已确认，无需再次确认",
                )
            if data.get("created") is False:
                return Action(
                    "finish_material",
                    {"outcome": "duplicate"},
                    "幂等命中，草稿已存在，本次不产生新草稿",
                )
            if state.needs_confirmation:
                return Action(
                    "request_confirmation",
                    {"reason": "替代时段需要用户确认后才可定稿"},
                    "草稿已按替代时段保存，但用户没有同意改期，必须停在待确认",
                )
            return Action(
                "finish_material",
                {"outcome": "pass"},
                "时段与用户要求一致且草稿已保存，本素材处理完毕",
            )

        # 8. 依据时段结果决定保存 / 换时段 / 请求确认 / 阻断
        slots = state.slots or {}
        available: List[str] = list(slots.get("available") or [])
        requested = state.spec.get("requested_time")
        constraint = (state.spec.get("time_constraint") or "preferred").lower()

        if requested is None:
            if not available:
                return Action(
                    "request_confirmation",
                    {"reason": "当天已无可用时段", "slot_status": "conflict_unresolved"},
                    "用户未指定时段且当天无可用时段，需要用户给新日期或放宽条件",
                )
            return Action(
                "save_draft",
                {"slot": available[0], "date": state.spec.get("date"), "needs_confirmation": False},
                "用户没有时段偏好，取最早可用时段即可，无需改期确认",
            )

        if requested in available:
            return Action(
                "save_draft",
                {"slot": requested, "date": state.spec.get("date"), "needs_confirmation": False},
                f"{requested} 空闲，与用户要求一致，直接保存草稿",
            )

        if constraint == "hard":
            return Action(
                "block",
                {
                    "reason": f"{requested} 已被占用，且用户声明该时段为硬性要求，不可改期",
                    "slot_status": "conflict_unresolved",
                },
                "硬约束不可替代；擅自改期等于变更用户需求",
            )

        alternative = pick_alternative(requested, available)
        if alternative is None:
            return Action(
                "request_confirmation",
                {"reason": f"{requested} 被占用且当天无任何可用替代时段", "slot_status": "conflict_unresolved"},
                "没有可行解，把选择权交回用户，而不是硬塞一个时段",
            )

        approved = state.approved_slot
        needs_confirmation = not (approved and approved == alternative)
        reason = (
            f"{requested} 被占用，{alternative} 可用；"
            + ("用户已确认该替代时段" if not needs_confirmation else "需用户确认后才能定稿")
        )
        return Action(
            "save_draft",
            {
                "slot": alternative,
                "date": state.spec.get("date"),
                "needs_confirmation": needs_confirmation,
            },
            reason,
        )
