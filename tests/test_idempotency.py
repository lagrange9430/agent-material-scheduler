"""幂等测试：重复提交只产生一份草稿，且跨进程（新 Agent 实例）成立。"""

from __future__ import annotations

import unittest

import tests.helpers as helpers
from scheduler_agent.store import DraftStore


class IdempotencyTest(helpers.TempRuntimeMixin, unittest.TestCase):
    def test_same_request_twice_creates_one_draft(self) -> None:
        first = helpers.run_input("a2-conflict", self.runtime)
        second = helpers.run_input("a2-conflict", self.runtime)

        self.assertEqual(first["materials"][0]["draft_status"], "saved")
        self.assertEqual(second["materials"][0]["draft_status"], "duplicate")
        self.assertEqual(
            first["materials"][0]["draft_id"], second["materials"][0]["draft_id"]
        )
        self.assertEqual(DraftStore(self.runtime).count(), 1, "重复提交不得产生第二份草稿")

    def test_duplicate_keeps_pending_confirmation(self) -> None:
        helpers.run_input("a2-conflict", self.runtime)
        second = helpers.run_input("a2-conflict", self.runtime)
        material = second["materials"][0]
        self.assertTrue(material["needs_user_confirmation"])
        self.assertTrue(material["pending_items"])
        self.assertEqual(second["status"], "needs_confirmation")

    def test_different_slot_creates_second_draft(self) -> None:
        helpers.run_input("a1-normal", self.runtime)
        task = dict(helpers.load_input("a1-normal"))
        task["task_id"] = "a1-alt-slot"
        task["materials"] = [{"material_id": "M02", "requested_time": "13:00"}]
        helpers.make_agent(self.runtime).run(task)
        self.assertEqual(DraftStore(self.runtime).count(), 2)

    def test_fresh_process_reads_existing_draft(self) -> None:
        helpers.run_input("a1-normal", self.runtime)
        # 模拟进程重启：全新的 store 实例仍能读到草稿并判定重复
        self.assertEqual(len(DraftStore(self.runtime).all()), 1)
        again = helpers.run_input("a1-normal", self.runtime)
        self.assertEqual(again["materials"][0]["draft_status"], "duplicate")

    def test_approval_updates_in_place_without_new_draft(self) -> None:
        helpers.run_input("a2-conflict", self.runtime)
        task = helpers.load_input("a2-conflict")
        task["options"]["approve"] = ["M01=19:00"]
        payload = helpers.make_agent(self.runtime).run(task).payload
        material = payload["materials"][0]

        self.assertEqual(material["draft_status"], "updated")
        self.assertFalse(material["needs_user_confirmation"])
        self.assertEqual(material["pending_items"], [])
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(DraftStore(self.runtime).count(), 1, "确认改期应就地更新，而不是新建草稿")

        records = DraftStore(self.runtime).all()
        self.assertEqual(records[0]["revision"], 2)
        self.assertEqual(records[0]["status"], "draft_confirmed")

    def test_approval_is_never_downgraded_by_later_request(self) -> None:
        helpers.run_input("a2-conflict", self.runtime)
        approved = helpers.load_input("a2-conflict")
        approved["options"]["approve"] = ["M01=19:00"]
        helpers.make_agent(self.runtime).run(approved)

        again = helpers.run_input("a2-conflict", self.runtime)
        material = again["materials"][0]
        self.assertEqual(material["draft_status"], "duplicate")
        self.assertFalse(material["needs_user_confirmation"], "已确认的草稿不得被改回待确认")


if __name__ == "__main__":
    unittest.main()
