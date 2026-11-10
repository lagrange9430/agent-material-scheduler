"""红线测试：不发布、不越权、不泄漏密钥、不静默改期。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import tests.helpers as helpers
from scheduler_agent.store import DraftStore
from scheduler_agent.tools import ToolRegistry

EXAMPLES = sorted((helpers.REPO / "examples").glob("*.input.json"))


class _ReschedulePolicy:
    """模拟一个试图给硬约束素材改期的决策层，验证护栏能拦住。"""

    mode = "test_reschedule"

    def next(self, state):
        from scheduler_agent.models import Action

        if not state.has("read_material"):
            return Action("read_material", {"material_id": state.material_id}, "读素材")
        if not state.has("check_rules"):
            return Action("check_rules", {"material_id": state.material_id}, "查规则")
        if not state.has("query_slots"):
            return Action("query_slots", {"date": "2026-11-10", "duration_min": 1}, "查时段")
        return Action(
            "save_draft",
            {"slot": "19:00", "date": "2026-11-10", "needs_confirmation": False},
            "擅自改期到 19:00",
        )


class SafetyTest(helpers.TempRuntimeMixin, unittest.TestCase):
    def test_only_four_tools_and_no_publish(self) -> None:
        handlers = [name for name in dir(ToolRegistry) if name.startswith("_tool_")]
        self.assertEqual(
            sorted(handlers),
            ["_tool_check_rules", "_tool_query_slots", "_tool_read_material", "_tool_save_draft"],
        )
        self.assertFalse(hasattr(ToolRegistry, "_tool_publish"))

    def test_no_publish_wording_in_any_output(self) -> None:
        self.assertTrue(EXAMPLES, "examples 目录下应有输入用例")
        for path in EXAMPLES:
            task = json.loads(path.read_text(encoding="utf-8"))
            agent = helpers.make_agent(self.runtime, faults=list(task.get("faults") or []))
            payload = agent.run(task).payload
            text = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn("published", text, f"{path.name} 出现了发布语义")
            self.assertNotIn("已发布", text, f"{path.name} 出现了发布语义")

    def test_hard_constraint_never_rescheduled(self) -> None:
        payload = helpers.make_agent(self.runtime).run(helpers.load_input("a7-hard-block")).payload
        material = payload["materials"][0]
        self.assertEqual(material["suggested_slot"], None)
        self.assertEqual(material["requested_slot"], "2026-11-10T20:00")
        self.assertEqual(DraftStore(self.runtime).count(), 0, "硬约束冲突不得留下草稿")

    def test_hard_constraint_guard_blocks_rescheduling_policy(self) -> None:
        from scheduler_agent.agent import Agent

        agent = Agent(
            _ReschedulePolicy(),
            helpers.DataStore(helpers.DATA_DIR),
            helpers.DraftStore(self.runtime),
        )
        payload = agent.run(helpers.load_input("a7-hard-block")).payload
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["materials"][0]["slot_status"], "conflict_unresolved")
        self.assertEqual(DraftStore(self.runtime).count(), 0)

    def test_failed_save_leaves_no_draft(self) -> None:
        payload = helpers.make_agent(self.runtime).run(helpers.load_input("a6-save-failed")).payload
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(DraftStore(self.runtime).count(), 0)

    def test_env_is_ignored_and_example_env_has_no_secret(self) -> None:
        gitignore = (helpers.REPO / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".env", gitignore)
        example = (helpers.REPO / ".env.example").read_text(encoding="utf-8")
        for line in example.splitlines():
            if line.startswith("DEEPSEEK_API_KEY="):
                self.assertEqual(line.strip(), "DEEPSEEK_API_KEY=", "示例文件不得包含真实密钥")

    def test_no_secret_in_generated_outputs(self) -> None:
        drafts = DraftStore(self.runtime)
        helpers.run_input("a1-normal", self.runtime)
        text = json.dumps(drafts.load(), ensure_ascii=False)
        self.assertNotIn("sk-", text)
        self.assertNotIn("Bearer", text)


if __name__ == "__main__":
    unittest.main()
