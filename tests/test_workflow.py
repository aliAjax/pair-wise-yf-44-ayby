import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _verify_all_items(self, change_id):
        items = self.repo.find_entities("action_item", "change_id", change_id)
        for item in items:
            if item["status"] == "open":
                self.service.transition(
                    self.actor,
                    item["id"],
                    "complete",
                    {"completed_by": "O-1", "evidence": "log-" + item["id"]},
                )
                self.service.transition(
                    self.actor, item["id"], "verify", {"verifier": "V-1"}
                )

    def test_full_workflow(self):
        unit = self.service.create(
            self.actor, "unit", {"name": "Reactor-1", "location": "Plant-A"}
        )
        window = self.service.create(
            self.actor,
            "window",
            {
                "unit_id": unit["id"],
                "start": "2026-10-10T08:00:00",
                "end": "2026-10-10T20:00:00",
                "capacity": 1,
            },
        )
        change = self.service.create(
            self.actor,
            "change",
            {"unit_id": unit["id"], "description": "Change alarm threshold"},
        )

        assessed = self.service.transition(
            self.actor,
            change["id"],
            "assess",
            {"risk_level": "medium", "analyst": "E-1"},
        )
        self.assertEqual(assessed["status"], "assessed")
        # 评估按风险等级自动派生措施行动项
        self.assertEqual(
            sorted(assessed["data"]["required_measures"]),
            ["site_briefing", "work_permit"],
        )
        self._verify_all_items(change["id"])

        approved = self.service.transition(
            self.actor,
            change["id"],
            "approve",
            {"approvals": ["S-1", "S-2"], "permit_id": "MOC-1"},
        )
        self.assertEqual(approved["status"], "approved")

        # 没有窗口不允许实施
        from src.domain import ValidationError

        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor, change["id"], "implement", {"procedure_version": "v2"}
            )

        booking, state = self.service.submit_window(
            self.actor,
            window["id"],
            {"change_id": change["id"]},
            idem_key="booking-1",
        )
        self.assertEqual(state, "scheduled")

        implemented = self.service.transition(
            self.actor, change["id"], "implement", {"procedure_version": "v2"}
        )
        self.assertEqual(implemented["status"], "implemented")
        self.assertEqual(implemented["data"]["implementation_state"], "fulfilled")

        # 实施后新增的行动项也必须复核通过才能投产
        extra = self.service.create(
            self.actor,
            "action_item",
            {"change_id": change["id"], "description": "Train operators", "owner": "O-1"},
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor, change["id"], "commission", {"tests_passed": True}
            )
        self.service.transition(
            self.actor,
            extra["id"],
            "complete",
            {"completed_by": "O-1", "evidence": "training-log"},
        )
        self.service.transition(
            self.actor, extra["id"], "verify", {"verifier": "V-1"}
        )

        commissioned = self.service.transition(
            self.actor, change["id"], "commission", {"tests_passed": True}
        )
        self.assertEqual(commissioned["status"], "commissioned")
        # 投产即离开窗口，booking 释放
        self.assertEqual(
            self.repo.get_entity(booking["id"])["status"], "released"
        )

        rolled = self.service.transition(
            self.actor,
            change["id"],
            "rollback",
            {"reason": "unexpected drift"},
        )
        self.assertEqual(rolled["status"], "rolled_back")


if __name__ == "__main__":
    unittest.main()
