"""验收矩阵 A1~A8 + 对照实验 C1。每条对应 docs/03-evaluation.md 的一行。"""

from __future__ import annotations

import json
import unittest
from typing import Any, Dict

import tests.helpers as helpers

REQUIRED_KEYS = {
    "material_id",
    "review_status",
    "risk_reasons",
    "requested_slot",
    "suggested_slot",
    "slot_status",
    "needs_user_confirmation",
    "pending_items",
    "draft_status",
    "draft_id",
    "conclusion",
}


class AcceptanceTest(helpers.TempRuntimeMixin, unittest.TestCase):
    def _run(self, name: str) -> Dict[str, Any]:
        return helpers.make_agent(self.runtime).run(helpers.load_input(name)).payload

    def _material(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return payload["materials"][0]

    # A1 ---------------------------------------------------------------
    def test_a1_normal_schedule(self) -> None:
        payload = self._run("a1-normal")
        material = self._material(payload)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(material["review_status"], "pass")
        self.assertEqual(material["slot_status"], "available")
        self.assertEqual(material["draft_status"], "saved")
        self.assertEqual(material["suggested_slot"], "2026-11-10T10:00")
        self.assertTrue(material["draft_id"].startswith("D-"))
        self.assertFalse(material["needs_user_confirmation"])

    # A2 ---------------------------------------------------------------
    def test_a2_conflict_needs_confirmation(self) -> None:
        payload = self._run("a2-conflict")
        material = self._material(payload)
        self.assertEqual(payload["status"], "needs_confirmation")
        self.assertEqual(material["slot_status"], "conflict_resolved")
        self.assertEqual(material["requested_slot"], "2026-11-10T18:00")
        self.assertEqual(material["suggested_slot"], "2026-11-10T19:00", "应建议 19:00 而不是沿用被占用的 18:00")
        self.assertTrue(material["needs_user_confirmation"])
        self.assertTrue(material["pending_items"])
        self.assertEqual(material["draft_status"], "saved")
        self.assertEqual(material["deviation"]["reason"], "requested slot occupied")
        self.assertNotIn("已发布", json.dumps(payload, ensure_ascii=False))

    # A3 ---------------------------------------------------------------
    def test_a3_info_gap(self) -> None:
        payload = self._run("a3-info-gap")
        material = self._material(payload)
        self.assertEqual(material["review_status"], "info_gap")
        self.assertEqual(material["slot_status"], "not_checked")
        self.assertEqual(material["draft_status"], "not_saved")
        self.assertIsNone(material["draft_id"])
        self.assertIn("duration_seconds", material["conclusion"])

    # A4 ---------------------------------------------------------------
    def test_a4_rule_risk(self) -> None:
        payload = self._run("a4-rule-risk")
        material = self._material(payload)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(material["review_status"], "risk")
        ids = {item["rule_id"] for item in material["risk_reasons"]}
        self.assertTrue({"R01", "R02"}.issubset(ids))
        self.assertEqual(material["draft_status"], "not_saved")
        for item in material["risk_reasons"]:
            self.assertTrue(item["evidence"], "每条风险都必须带原文证据")

    # A5 ---------------------------------------------------------------
    def test_a5_duplicate_submission(self) -> None:
        helpers.run_input("a2-conflict", self.runtime)
        payload = self._run("a2-conflict")
        material = self._material(payload)
        self.assertEqual(material["draft_status"], "duplicate")
        self.assertEqual(payload["status"], "needs_confirmation")
        from scheduler_agent.store import DraftStore

        self.assertEqual(DraftStore(self.runtime).count(), 1)

    # A6 ---------------------------------------------------------------
    def test_a6_save_failed(self) -> None:
        payload = self._run("a6-save-failed")
        material = self._material(payload)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(material["draft_status"], "failed")
        self.assertIsNone(material["draft_id"])
        self.assertIn("保存失败", material["conclusion"])

    # A7 ---------------------------------------------------------------
    def test_a7_hard_constraint_blocks_without_rescheduling(self) -> None:
        payload = self._run("a7-hard-block")
        material = self._material(payload)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(material["slot_status"], "conflict_unresolved")
        self.assertIsNone(material["suggested_slot"], "硬约束下不得给出替代时段")
        self.assertEqual(material["draft_status"], "not_saved")
        self.assertFalse(material["needs_user_confirmation"])

    # A8 ---------------------------------------------------------------
    def test_a8_step_limit_keeps_findings(self) -> None:
        payload = self._run("a8-step-limit")
        material = self._material(payload)
        self.assertEqual(payload["status"], "incomplete")
        self.assertIsNotNone(payload["incomplete_reason"])
        self.assertEqual(material["review_status"], "risk")
        self.assertGreaterEqual(len(material["risk_reasons"]), 2)

    # C1 ---------------------------------------------------------------
    def test_c1_control_changes_action_sequence(self) -> None:
        conflicted = helpers.make_agent(self.runtime).run(helpers.load_input("a2-conflict"))
        freed = helpers.make_agent(self.runtime).run(helpers.load_input("c1-control-free"))

        conflicted_actions = [row["action"] for row in conflicted.trace_rows]
        freed_actions = [row["action"] for row in freed.trace_rows]

        self.assertIn("request_confirmation", conflicted_actions)
        self.assertNotIn("request_confirmation", freed_actions)
        self.assertNotEqual(conflicted_actions, freed_actions, "仅释放 18:00 就应改变动作序列")

        freed_material = freed.payload["materials"][0]
        self.assertEqual(freed_material["suggested_slot"], "2026-11-10T18:00")
        self.assertEqual(freed_material["slot_status"], "available")
        self.assertFalse(freed_material["needs_user_confirmation"])

    # 契约 -------------------------------------------------------------
    def test_output_contract_keys(self) -> None:
        for name in ("a1-normal", "a2-conflict", "a3-info-gap", "a4-rule-risk"):
            payload = self._run(name)
            for material in payload["materials"]:
                missing = REQUIRED_KEYS - set(material)
                self.assertEqual(missing, set(), f"{name} 缺少字段 {missing}")


if __name__ == "__main__":
    unittest.main()
