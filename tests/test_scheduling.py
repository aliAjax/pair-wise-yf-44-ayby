import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class SchedulingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.engineer = Actor("eng", "engineer")

    def tearDown(self):
        self.tmp.cleanup()

    def _unit(self, name="U-1"):
        return self.service.create(self.admin, "unit", {"name": name, "location": "Plant-A"})

    def _window(self, unit_id, start="2026-10-10T08:00", end="2026-10-10T18:00", capacity=1):
        return self.service.create(
            self.admin,
            "window",
            {"unit_id": unit_id, "start": start, "end": end, "capacity": capacity},
        )

    def _approved_change(self, unit_id, desc="Change", risk="low"):
        change = self.service.create(
            self.engineer, "change", {"unit_id": unit_id, "description": desc}
        )
        self.service.transition(
            self.engineer, change["id"], "assess",
            {"risk_level": risk, "analyst": "E-1"},
        )
        self.service.transition(
            Actor("safety", "safety"), change["id"], "approve",
            {"approvals": ["S-1"], "permit_id": "P-1"},
        )
        return self.service.get(change["id"])

    def _schedule(self, change_id, window_id, idem_key=None):
        data = {"window_id": window_id}
        if idem_key:
            data["idempotency_key"] = idem_key
        return self.service.transition(self.engineer, change_id, "schedule", data)

    def test_schedule_ledger_connects_unit_change_window(self):
        unit = self._unit()
        window = self._window(unit["id"], capacity=2)
        change = self._approved_change(unit["id"])
        updated = self._schedule(change["id"], window["id"])
        self.assertEqual(updated["data"]["schedule_status"], "scheduled")
        self.assertEqual(updated["data"]["window_id"], window["id"])
        ledger = self.service.list("schedule")
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["data"]["change_id"], change["id"])
        self.assertEqual(ledger[0]["data"]["window_id"], window["id"])
        self.assertEqual(ledger[0]["status"], "scheduled")

    def test_full_window_queues_new_application(self):
        unit = self._unit()
        window = self._window(unit["id"], capacity=1)
        first = self._approved_change(unit["id"], "first")
        second = self._approved_change(unit["id"], "second")
        self._schedule(first["id"], window["id"])
        queued = self._schedule(second["id"], window["id"])
        self.assertEqual(queued["data"]["schedule_status"], "queued")
        schedules = self.service.list("schedule")
        statuses = {s["data"]["change_id"]: s["status"] for s in schedules}
        self.assertEqual(statuses[first["id"]], "scheduled")
        self.assertEqual(statuses[second["id"]], "queued")
        window_after = self.service.get(window["id"])
        self.assertEqual(window_after["data"]["scheduled_count"], 1)

    def test_recover_queued_in_application_order(self):
        unit = self._unit()
        window = self._window(unit["id"], capacity=1)
        first = self._approved_change(unit["id"], "first")
        second = self._approved_change(unit["id"], "second")
        third = self._approved_change(unit["id"], "third")
        self._schedule(first["id"], window["id"])
        self._schedule(second["id"], window["id"])
        self._schedule(third["id"], window["id"])
        # 释放一个窗口容量，再按申请顺序恢复。
        self.service.repository.claim_window_slot  # noqa: B018 (ensure method exists)
        window_entity = self.service.get(window["id"])
        window_data = dict(window_entity["data"])
        window_data["scheduled_count"] = 0
        self.service.repository.update_entity(
            window["id"], window_entity["version"], window_entity["status"], window_data
        )
        result = self.service.recover_schedule(self.admin)
        self.assertEqual(result["recovered"], 1)
        second_after = self.service.get(second["id"])
        self.assertEqual(second_after["data"]["schedule_status"], "scheduled")
        third_after = self.service.get(third["id"])
        self.assertEqual(third_after["data"]["schedule_status"], "queued")
        # 排程账中第二条排队记录已转为已排程。
        second_sched = [
            s for s in self.service.list("schedule")
            if s["data"]["change_id"] == second["id"]
        ][0]
        self.assertEqual(second_sched["status"], "scheduled")

    def test_retry_does_not_double_occupy_slot(self):
        unit = self._unit()
        window = self._window(unit["id"], capacity=1)
        change = self._approved_change(unit["id"])
        first = self._schedule(change["id"], window["id"], idem_key="retry-1")
        second = self._schedule(change["id"], window["id"], idem_key="retry-1")
        self.assertEqual(first["data"]["window_id"], second["data"]["window_id"])
        window_after = self.service.get(window["id"])
        self.assertEqual(window_after["data"]["scheduled_count"], 1)
        schedules = self.service.list("schedule")
        self.assertEqual(len(schedules), 1)

    def test_duplicate_schedule_without_key_conflicts(self):
        unit = self._unit()
        window = self._window(unit["id"], capacity=2)
        change = self._approved_change(unit["id"])
        self._schedule(change["id"], window["id"])
        with self.assertRaises(ConflictError):
            self._schedule(change["id"], window["id"])

    def test_implement_requires_all_items_verified(self):
        unit = self._unit()
        change = self._approved_change(unit["id"])
        item = self.service.create(
            self.admin, "action_item",
            {"change_id": change["id"], "description": "measure", "owner": "O-1"},
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.engineer, change["id"], "implement",
                {"procedure_version": "v1"},
            )
        self.service.transition(
            self.engineer, item["id"], "complete",
            {"completed_by": "O-1", "evidence": "log"},
        )
        self.service.transition(
            Actor("verifier", "verifier"), item["id"], "verify", {"verifier": "V-1"}
        )
        done = self.service.transition(
            self.engineer, change["id"], "implement", {"procedure_version": "v1"}
        )
        self.assertEqual(done["status"], "implemented")

    def test_reopen_item_reverts_implemented_to_approved(self):
        unit = self._unit()
        change = self._approved_change(unit["id"])
        item = self.service.create(
            self.admin, "action_item",
            {"change_id": change["id"], "description": "measure", "owner": "O-1"},
        )
        self.service.transition(
            self.engineer, item["id"], "complete",
            {"completed_by": "O-1", "evidence": "log"},
        )
        self.service.transition(
            Actor("verifier", "verifier"), item["id"], "verify", {"verifier": "V-1"}
        )
        self.service.transition(
            self.engineer, change["id"], "implement", {"procedure_version": "v1"}
        )
        reverted = self.service.transition(
            Actor("verifier", "verifier"), item["id"], "reopen", {"reason": "retest"}
        )
        self.assertEqual(reverted["status"], "open")
        change_after = self.service.get(change["id"])
        self.assertEqual(change_after["status"], "approved")
        self.assertTrue(change_after["data"]["reeval_pending"])

    def test_risk_change_voids_unverified_and_recalculates(self):
        unit = self._unit()
        change = self.service.create(
            self.engineer, "change", {"unit_id": unit["id"], "description": "risk"}
        )
        self.service.transition(
            self.engineer, change["id"], "assess",
            {"risk_level": "low", "analyst": "E-1"},
        )
        item = self.service.create(
            self.admin, "action_item",
            {"change_id": change["id"], "description": "old measure", "owner": "O-1"},
        )
        self.service.transition(
            self.engineer, item["id"], "complete",
            {"completed_by": "O-1", "evidence": "log"},
        )
        self.service.transition(
            self.engineer, change["id"], "assess",
            {"risk_level": "high", "analyst": "E-2"},
        )
        items = self.service.list("action_item")
        change_items = [i for i in items if i["data"]["change_id"] == change["id"]]
        self.assertFalse(any(i["id"] == item["id"] for i in change_items))
        self.assertEqual(len(change_items), 3)
        self.assertTrue(all(i["status"] == "open" for i in change_items))

    def test_commissioned_change_keeps_records_on_risk_change(self):
        unit = self._unit()
        change = self.service.create(
            self.engineer, "change", {"unit_id": unit["id"], "description": "risk"}
        )
        self.service.transition(
            self.engineer, change["id"], "assess",
            {"risk_level": "low", "analyst": "E-1"},
        )
        self.service.transition(
            Actor("safety", "safety"), change["id"], "approve",
            {"approvals": ["S-1"], "permit_id": "P-1"},
        )
        item = self.service.create(
            self.admin, "action_item",
            {"change_id": change["id"], "description": "measure", "owner": "O-1"},
        )
        self.service.transition(
            self.engineer, item["id"], "complete",
            {"completed_by": "O-1", "evidence": "log"},
        )
        self.service.transition(
            Actor("verifier", "verifier"), item["id"], "verify", {"verifier": "V-1"}
        )
        self.service.transition(
            self.engineer, change["id"], "implement", {"procedure_version": "v1"}
        )
        self.service.transition(
            self.engineer, change["id"], "commission", {"tests_passed": True}
        )
        before = self.service.list("action_item")
        self.service.transition(
            self.engineer, change["id"], "assess",
            {"risk_level": "critical", "analyst": "E-2"},
        )
        after = self.service.list("action_item")
        self.assertEqual(len(before), len(after))
        self.assertEqual(
            [i["id"] for i in before], [i["id"] for i in after]
        )

    def test_backfill_implementation_status_from_review_records(self):
        unit = self._unit()
        change = self.service.create(
            self.engineer, "change", {"unit_id": unit["id"], "description": "legacy"}
        )
        item = self.service.create(
            self.admin, "action_item",
            {"change_id": change["id"], "description": "measure", "owner": "O-1"},
        )
        self.service.transition(
            self.engineer, item["id"], "complete",
            {"completed_by": "O-1", "evidence": "log"},
        )
        self.service.transition(
            Actor("verifier", "verifier"), item["id"], "verify", {"verifier": "V-1"}
        )
        result = self.service.backfill(self.admin)
        self.assertEqual(result["backfilled"], 1)
        change_after = self.service.get(change["id"])
        self.assertEqual(change_after["data"]["implementation_status"], "confirmed")
        # 历史变更仍能打开。
        reopened = self.service.get(change["id"])
        self.assertEqual(reopened["id"], change["id"])


if __name__ == "__main__":
    unittest.main()
