"""真实模型接入：OpenAI 兼容 /chat/completions，只依赖标准库 urllib。

模型只做一件事：根据观测选下一个动作。工具执行、预算、状态机都不归它管。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .agent import PolicyError
from .models import ALL_ACTIONS, Action, MaterialState

SYSTEM_PROMPT = """你是内容团队的「素材审核与发布排期 Agent」。你在一个循环里工作：每轮选择一个动作，
系统执行它并把结果作为 observations 返回给你，你再决定下一步。

硬性规则：
1. 每轮只输出一个 JSON 对象，不要解释、不要 markdown 代码块：
   {"action": "...", "args": {...}, "reason": "一句话依据"}
2. action 只能取：read_material / check_rules / query_slots / save_draft /
   request_confirmation / block / finish_material
3. 只能依据 observations 里已经发生的事实决策，不得虚构工具返回。
4. 只能从 available_slots 中选择时段；不在列表里的时段一律视为占用。
5. 系统只保存草稿，禁止声称「已发布」。
6. 当 requested_time 已被占用且 time_constraint=hard 时，必须 block，不得替换时段。
7. 关键字段缺失时不得猜测；信息不足以判定时用 finish_material（outcome=info_gap）。
8. save_draft 失败后不要再次调用 save_draft，用 finish_material（outcome=save_failed）如实上报。
9. 素材已给出结论后，用终止动作结束该素材，不要继续调用工具。

动作参数：
  read_material       {"material_id": "M01"}
  check_rules         {"material_id": "M01"}
  query_slots         {"date": "2026-11-10", "duration_min": 1}
  save_draft          {"slot": "19:00", "date": "2026-11-10", "needs_confirmation": true}
  request_confirmation {"reason": "..."}
  block               {"reason": "..."}
  finish_material     {"outcome": "pass" | "info_gap" | "save_failed" | "duplicate"}
"""


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = 60.0) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def chat_json(self, system: str, user: str, max_tokens: int = 400) -> Dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise LLMError(f"模型接口返回 HTTP {exc.code}：{detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"模型接口不可达：{exc.reason}") from exc
        except TimeoutError as exc:
            raise LLMError("模型调用超时") from exc

        content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        return _parse_json_object(content)


def _parse_json_object(content: str) -> Dict[str, Any]:
    """容忍代码块包裹与多余尾随文本，只取第一个完整 JSON 对象。"""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    if start == -1:
        raise LLMError(f"模型输出不含 JSON 对象：{text[:200]!r}")
    try:
        parsed, _end = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise LLMError(f"模型输出 JSON 解析失败：{exc}；原文：{text[:200]!r}") from exc
    if not isinstance(parsed, dict):
        raise LLMError("模型输出的 JSON 顶层不是对象")
    return parsed


class LLMPolicy:
    """把状态渲染成 prompt，把模型输出校验成 Action。"""

    def __init__(self, client: LLMClient, max_repairs: int = 1) -> None:
        self.client = client
        self.max_repairs = max_repairs
        self.mode = f"llm:{client.model}"

    def next(self, state: MaterialState) -> Action:
        payload = json.dumps(self._render(state), ensure_ascii=False)
        last_error = ""
        for attempt in range(self.max_repairs + 1):
            user = payload
            if attempt > 0:
                user = (
                    payload
                    + "\n\n上一次输出无效："
                    + last_error
                    + "\n请重新只输出一个符合 schema 的 JSON 对象。"
                )
            try:
                raw = self.client.chat_json(SYSTEM_PROMPT, user)
                action = self._to_action(raw)
                state.repairs += attempt
                return action
            except LLMError as exc:
                last_error = str(exc)
        raise PolicyError(f"模型连续 {self.max_repairs + 1} 次未给出可解析动作：{last_error}")

    # ------------------------------------------------------------------
    def _to_action(self, raw: Dict[str, Any]) -> Action:
        name = raw.get("action")
        if name not in ALL_ACTIONS:
            raise LLMError(f"动作名非法：{name!r}")
        args = raw.get("args") or {}
        if not isinstance(args, dict):
            raise LLMError("args 必须是对象")
        # 只允许模型输出白名单里的动作；参数由工具再次校验
        for key in list(args.keys()):
            if key not in ("material_id", "date", "duration_min", "slot", "needs_confirmation",
                           "reason", "outcome", "slot_status"):
                args.pop(key, None)
        return Action(str(name), args, str(raw.get("reason") or ""))

    def _render(self, state: MaterialState) -> Dict[str, Any]:
        observations = [
            {
                "action": obs["action"],
                "ok": obs["ok"],
                "error_code": obs.get("error_code"),
                "summary": obs["summary"],
            }
            for obs in state.observations
        ]
        available: List[str] = []
        if state.slots:
            available = list(state.slots.get("available") or [])
        return {
            "task": {
                "date": state.spec.get("date"),
                "material_id": state.material_id,
                "requested_time": state.spec.get("requested_time"),
                "time_constraint": state.spec.get("time_constraint") or "preferred",
                "approved_slot": state.approved_slot,
                "note": state.spec.get("note"),
            },
            "observations": observations[-8:],
            "available_slots": available,
            "budget": {"steps_used": state.steps, "tool_calls_used": state.tool_calls},
            "hint": "选择一个动作。若已可下结论，请使用终止动作。",
        }


def check_model(client: LLMClient) -> Dict[str, Any]:
    """连通性自检：不打印密钥，只回报模型名与延迟。"""
    import time

    started = time.time()
    try:
        raw = client.chat_json(SYSTEM_PROMPT, '{"task":{"date":"2026-11-10","material_id":"M01"}}', max_tokens=64)
    except LLMError as exc:
        return {"ok": False, "model": client.model, "error": str(exc)}
    return {
        "ok": True,
        "model": client.model,
        "latency_s": round(time.time() - started, 2),
        "sample": raw,
    }
