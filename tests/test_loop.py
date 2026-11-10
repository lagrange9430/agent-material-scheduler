"""循环与预算测试：上限必须生效，且不能吞掉已获得的结论。"""

from __future__ import annotations

import unittest
from typing import Any, Dict

import tests.helpers as helpers
from scheduler_agent.agent import Agent, PolicyError
from scheduler_agent.models import Action


class _BadPolicy:
    mode = "test_bad"

    def next(self, state: Any) -> Action:
        return Action("publish", {"slot": "18:00"}, "试图越权发布")


class _ExplodingPolicy:
    mode = "test_explode"

    def next(self, state: Any) -> Action:
        raise RuntimeError("模型超时")


class LoopBudgetTest(helpers.TempRuntimeMixin, unittest.TestCase):
    def test_step_budget_returns_incomplete_and_keeps_findings(self) -> None:
        payload = helpers.run_input("a8-step-limit", self.runtime)
        self.assertEqual(payload["status"], "incomplete")
        self.assertIn("step_budget", payload["incomplete_reason"])
        material = payload["materials"][0]
        self.assertEqual(material["review_status"], "risk", "已查出的风险不能因为超限被丢弃")
        self.assertGreaterEqual(len(material["risk_reasons"]), 2)
        self.assertEqual(material["draft_status"], "not_saved")
        self.assertEqual(payload["steps_used"], 2)

    def test_tool_call_budget_is_independent(self) -> None:
        task: Dict[str, Any] = {
            "task_id": "tool-budget",
            "date": "2026-11-10",
            "materials": [{"material_id": "M01", "requested_time": "10:00"}],
            "options": {"max_steps": 8, "max_tool_calls": 2},
        }
        agent = helpers.make_agent(self.runtime)
        payload = agent.run(task).payload
        self.assertEqual(payload["status"], "incomplete")
        self.assertIn("tool_budget", payload["incomplete_reason"])
        self.assertEqual(payload["tool_calls_used"], 2)
        self.assertEqual(payload["materials"][0]["draft_status"], "not_saved")

    def test_terminal_action_does_not_consume_tool_budget(self) -> None:
        agent = helpers.make_agent(self.runtime)
        payload = agent.run(helpers.load_input("a2-conflict")).payload
        self.assertEqual(payload["steps_used"], 5)
        self.assertEqual(payload["tool_calls_used"], 4, "终止动作不应计入工具调用")

    def test_illegal_action_is_rejected(self) -> None:
        agent = Agent(
            _BadPolicy(),
            helpers.DataStore(helpers.DATA_DIR),
            helpers.DraftStore(self.runtime),
        )
        with self.assertRaises(PolicyError):
            agent.run(helpers.load_input("a1-normal"))

    def test_policy_crash_surfaces_as_policy_error(self) -> None:
        agent = Agent(
            _ExplodingPolicy(),
            helpers.DataStore(helpers.DATA_DIR),
            helpers.DraftStore(self.runtime),
        )
        with self.assertRaises(PolicyError):
            agent.run(helpers.load_input("a1-normal"))

    def test_invalid_input_fails_fast(self) -> None:
        agent = helpers.make_agent(self.runtime)
        with self.assertRaises(Exception):
            agent.run({"task_id": "bad", "date": "2026-13-40", "materials": []})


if __name__ == "__main__":
    unittest.main()
