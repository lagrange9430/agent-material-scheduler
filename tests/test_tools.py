"""工具契约测试：成功、失败、注入故障、幂等、冲突拒绝。"""

from __future__ import annotations

import unittest

import tests.helpers as helpers
from scheduler_agent.models import Action, MaterialState
from scheduler_agent.store import DraftStore
from scheduler_agent.tools import ToolRegistry


class ToolContractTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.registry = ToolRegistry(helpers.DataStore(helpers.DATA_DIR), DraftStore(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _state(self, material_id: str) -> MaterialState:
        state = MaterialState({"material_id": material_id, "date": "2026-11-10"})
        return state

    def test_read_material_ok(self) -> None:
        state = self._state("M01")
        obs = self.registry.call(Action("read_material", {"material_id": "M01"}), state)
        self.assertTrue(obs.ok)
        self.assertEqual(obs.data["material"]["material_id"], "M01")
        self.assertEqual(obs.data["missing_fields"], [])

    def test_read_material_not_found(self) -> None:
        state = self._state("M99")
        obs = self.registry.call(Action("read_material", {"material_id": "M99"}), state)
        self.assertFalse(obs.ok)
        self.assertEqual(obs.error_code, "NOT_FOUND")

    def test_read_material_missing_fields_keeps_partial_data(self) -> None:
        state = self._state("M03")
        obs = self.registry.call(Action("read_material", {"material_id": "M03"}), state)
        self.assertFalse(obs.ok)
        self.assertEqual(obs.error_code, "MISSING_FIELDS")
        self.assertIsNotNone(obs.data["material"], "缺字段时仍应带回可用元数据")
        self.assertIn("duration_seconds", obs.data["missing_fields"])
        self.assertIn("copyright.scope", obs.data["missing_fields"])

    def test_check_rules_finds_blockers(self) -> None:
        state = self._state("M04")
        obs = self.registry.call(Action("read_material", {"material_id": "M04"}), state)
        state.observe("read_material", obs)
        result = self.registry.call(Action("check_rules", {"material_id": "M04"}), state)
        self.assertTrue(result.ok)
        ids = {v["rule_id"] for v in result.data["violations"]}
        self.assertEqual(ids, {"R01", "R02", "R05"})
        self.assertTrue(all(v["evidence"] for v in result.data["violations"]))

    def test_check_rules_skips_undecidable(self) -> None:
        state = self._state("M03")
        obs = self.registry.call(Action("read_material", {"material_id": "M03"}), state)
        state.observe("read_material", obs)
        result = self.registry.call(Action("check_rules", {"material_id": "M03"}), state)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["violations"], [])
        skipped = {s["rule_id"] for s in result.data["skipped"]}
        self.assertEqual(skipped, {"R01", "R03"})

    def test_query_slots_reports_occupancy(self) -> None:
        state = self._state("M01")
        obs = self.registry.call(
            Action("query_slots", {"date": "2026-11-10", "duration_min": 1}), state
        )
        self.assertTrue(obs.ok)
        occupied = {row["slot"]: row["material_id"] for row in obs.data["occupied"]}
        self.assertEqual(occupied["18:00"], "M00")
        self.assertIn("19:00", obs.data["available"])
        self.assertNotIn("18:00", obs.data["available"])

    def test_query_slots_invalid_date(self) -> None:
        state = self._state("M01")
        obs = self.registry.call(Action("query_slots", {"date": "2026-13-40"}), state)
        self.assertFalse(obs.ok)
        self.assertEqual(obs.error_code, "INVALID_DATE")

    def test_save_draft_rejects_occupied_slot(self) -> None:
        state = self._state("M01")
        obs = self.registry.call(Action("save_draft", {"slot": "18:00", "date": "2026-11-10"}), state)
        self.assertFalse(obs.ok)
        self.assertEqual(obs.error_code, "CONFLICT_SLOT")

    def test_save_draft_is_idempotent(self) -> None:
        state = self._state("M01")
        action = Action("save_draft", {"slot": "19:00", "date": "2026-11-10"})
        first = self.registry.call(action, state)
        second = self.registry.call(action, state)
        self.assertTrue(first.ok and second.ok)
        self.assertTrue(first.data["created"])
        self.assertFalse(second.data["created"])
        self.assertEqual(first.data["draft_id"], second.data["draft_id"])
        self.assertEqual(DraftStore(self._tmp.name).count(), 1)

    def test_injected_failure(self) -> None:
        registry = ToolRegistry(
            helpers.DataStore(helpers.DATA_DIR), DraftStore(self._tmp.name), faults={"M05:save_draft"}
        )
        state = self._state("M05")
        obs = registry.call(Action("save_draft", {"slot": "11:00", "date": "2026-11-10"}), state)
        self.assertFalse(obs.ok)
        self.assertEqual(obs.error_code, "SAVE_FAILED")
        self.assertIn("重复调用", obs.message)

    def test_unknown_tool_is_rejected(self) -> None:
        state = self._state("M01")
        obs = self.registry.call(Action("read_material", {"material_id": "M01"}), state)
        self.assertTrue(obs.ok)
        # 非工具动作交给上层处理，工具层明确报错
        obs2 = self.registry.call(Action("block"), state)
        self.assertFalse(obs2.ok)
        self.assertEqual(obs2.error_code, "NOT_A_TOOL")


if __name__ == "__main__":
    unittest.main()
