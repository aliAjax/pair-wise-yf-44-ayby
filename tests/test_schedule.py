import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine, required_measures
from src.service import DomainService


def make_service(path):
    repo = SQLiteRepository(path)
    return DomainService(repo, RuleEngine()), repo


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.service, self.repo = make_service(self.db)
        self.admin = Actor("admin", "admin")
        self.engineer = Actor("eng-1", "engineer")
        self.unit = self.service.create(
            self.admin, "unit", {"name": "U-1", "location": "Plant-A"}
        )
        self.window = self.service.create(
            self.admin,
            "window",
            {
                "unit_id": self.unit["id"],
                "start": "2026-10-10T08:00:00",
                "end": "2026-10-10T20:00:00",
                "capacity": 1,
            },
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _approved_change(self, tag, risk="low"):
        change = self.service.create(
            self.engineer,
            "change",
            {"unit_id": self.unit["id"], "description": "change " + tag},
        )
        self.service.transition(
            self.admin,
            change["id"],
            "assess",
            {"risk_level": risk, "analyst": "A"},
        )
        for item in self.repo.find_entities("action_item", "change_id", change["id"]):
            self.service.transition(
                self.admin,
                item["id"],
                "complete",
                {"completed_by": "O", "evidence": "e"},
            )
            self.service.transition(
                self.admin, item["id"], "verify", {"verifier": "V"}
            )
        approvals = ["S-1"] if risk == "low" else ["S-1", "S-2"]
        self.service.transition(
            self.admin,
            change["id"],
            "approve",
            {"approvals": approvals, "permit_id": "P-" + tag},
        )
        return change

    def test_full_window_books_rest_queue_in_application_order(self):
        c1 = self._approved_change("1")
        c2 = self._approved_change("2")
        c3 = self._approved_change("3")
        _, s1 = self.service.submit_window(
            self.engineer, self.window["id"], {"change_id": c1["id"]}, "k1"
        )
        _, s2 = self.service.submit_window(
            self.engineer, self.window["id"], {"change_id": c2["id"]}, "k2"
        )
        _, s3 = self.service.submit_window(
            self.engineer, self.window["id"], {"change_id": c3["id"]}, "k3"
        )
        self.assertEqual((s1, s2, s3), ("scheduled", "queued", "queued"))
        schedule = self.service.window_schedule(self.window["id"])
        self.assertEqual([b["data"]["change_id"] for b in schedule], [c1["id"], c2["id"], c3["id"]])

    def test_retry_does_not_occupy_or_duplicate(self):
        c1 = self._approved_change("1")
        c2 = self._approved_change("2")
        self.service.submit_window(
            self.engineer, self.window["id"], {"change_id": c1["id"]}, "k1"
        )
        first, _ = self.service.submit_window(
            self.engineer, self.window["id"], {"change_id": c2["id"]}, "k2"
        )
        # 满员时恢复：队首放不下，不占位
        self.assertEqual(self.service.recover_window(self.admin, self.window["id"]), [])
        # 带同一申请键重试：返回原记录，不新增
        retry, state = self.service.submit_window(
            self.engineer, self.window["id"], {"change_id": c2["id"]}, "k2"
        )
        self.assertEqual(state, "existing")
        self.assertEqual(retry["id"], first["id"])
        self.assertEqual(len(self.service.window_schedule(self.window["id"])), 2)

    def test_recovery_promotes_in_application_order(self):
        c1 = self._approved_change("1")
        c2 = self._approved_change("2")
        c3 = self._approved_change("3")
        b1, _ = self.service.submit_window(
            self.engineer, self.window["id"], {"change_id": c1["id"]}, "k1"
        )
        b2, _ = self.service.submit_window(
            self.engineer, self.window["id"], {"change_id": c2["id"]}, "k2"
        )
        b3, _ = self.service.submit_window(
            self.engineer, self.window["id"], {"change_id": c3["id"]}, "k3"
        )
        self.service.release_booking(self.admin, b1["id"], reason="done")
        self.assertEqual(self.repo.get_entity(b2["id"])["status"], "scheduled")
        self.assertEqual(self.repo.get_entity(b3["id"])["status"], "queued")

    def test_concurrent_submitters_first_wins_late_gets_conflict(self):
        c1 = self._approved_change("1")
        c2 = self._approved_change("2")
        seg = ("2026-10-10T08:00:00", "2026-10-10T20:00:00")
        from datetime import datetime

        seg_start, seg_end = datetime.fromisoformat(seg[0]), datetime.fromisoformat(seg[1])
        # 双方在取写锁前都看到有空位
        self.assertTrue(self.repo.window_has_room(self.window["id"], seg_start, seg_end))
        results = {}
        barrier = threading.Barrier(2)

        def submit(tag, change, key):
            barrier.wait()
            try:
                booking, state = self.repo.submit_booking(
                    self.window["id"],
                    change["id"],
                    seg_start,
                    seg_end,
                    "eng-" + tag,
                    key,
                    fit_before=True,
                )
                results[tag] = ("ok", state, booking["id"])
            except ConflictError as exc:
                results[tag] = ("conflict", str(exc))

        t1 = threading.Thread(target=submit, args=("1", c1, "race-1"))
        t2 = threading.Thread(target=submit, args=("2", c2, "race-2"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        statuses = sorted(results[k][0] for k in results)
        self.assertEqual(statuses, ["conflict", "ok"])
        winner = next(k for k in results if results[k][0] == "ok")
        self.assertEqual(results[winner][1], "scheduled")

    def test_change_segment_different_unit_rejected(self):
        other_unit = self.service.create(
            self.admin, "unit", {"name": "U-2", "location": "Plant-B"}
        )
        change = self.service.create(
            self.engineer,
            "change",
            {"unit_id": other_unit["id"], "description": "elsewhere"},
        )
        with self.assertRaises(ValidationError):
            self.service.submit_window(
                self.engineer, self.window["id"], {"change_id": change["id"]}
            )

    def test_window_validates_range_and_capacity(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                "window",
                {
                    "unit_id": self.unit["id"],
                    "start": "2026-10-11T08:00:00",
                    "end": "2026-10-10T20:00:00",
                    "capacity": 1,
                },
            )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                "window",
                {
                    "unit_id": self.unit["id"],
                    "start": "2026-10-10T08:00:00",
                    "end": "2026-10-10T20:00:00",
                    "capacity": 0,
                },
            )


class ReopenCascadeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.service, self.repo = make_service(self.db)
        self.admin = Actor("admin", "admin")
        self.unit = self.service.create(
            self.admin, "unit", {"name": "U-1", "location": "Plant-A"}
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _implement_change(self, window, risk="low"):
        change = self.service.create(
            self.admin,
            "change",
            {"unit_id": self.unit["id"], "description": "c"},
        )
        self.service.transition(
            self.admin, change["id"], "assess", {"risk_level": risk, "analyst": "A"}
        )
        items = self.repo.find_entities("action_item", "change_id", change["id"])
        for item in items:
            self.service.transition(
                self.admin,
                item["id"],
                "complete",
                {"completed_by": "O", "evidence": "e"},
            )
            self.service.transition(
                self.admin, item["id"], "verify", {"verifier": "V"}
            )
        self.service.transition(
            self.admin,
            change["id"],
            "approve",
            {"approvals": ["S-1"], "permit_id": "P"},
        )
        self.service.submit_window(
            self.admin, window["id"], {"change_id": change["id"]}, "b-" + change["id"]
        )
        self.service.transition(
            self.admin, change["id"], "implement", {"procedure_version": "v1"}
        )
        return change, items

    def test_reopen_after_implement_reverts_to_approved(self):
        window = self.service.create(
            self.admin,
            "window",
            {
                "unit_id": self.unit["id"],
                "start": "2026-10-10T08:00:00",
                "end": "2026-10-10T20:00:00",
                "capacity": 2,
            },
        )
        change, items = self._implement_change(window)
        item = items[0]
        self.service.transition(
            self.admin, item["id"], "reopen", {"reason": "evidence disputed"}
        )
        refreshed = self.repo.get_entity(change["id"])
        self.assertEqual(refreshed["status"], "approved")
        self.assertEqual(refreshed["data"]["evaluation_reset"]["reopened_item"], item["id"])
        # 未全部复核前不能再次实施
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, change["id"], "implement", {"procedure_version": "v2"}
            )
        # 重新评估/复核后才可再次实施（换一个窗口，旧的已被实施占用）
        window2 = self.service.create(
            self.admin,
            "window",
            {
                "unit_id": self.unit["id"],
                "start": "2026-10-11T08:00:00",
                "end": "2026-10-11T20:00:00",
                "capacity": 1,
            },
        )
        self.service.transition(
            self.admin,
            item["id"],
            "complete",
            {"completed_by": "O", "evidence": "e2"},
        )
        self.service.transition(
            self.admin, item["id"], "verify", {"verifier": "V2"}
        )
        self.service.submit_window(
            self.admin, window2["id"], {"change_id": change["id"]}, "b2"
        )
        again = self.service.transition(
            self.admin, change["id"], "implement", {"procedure_version": "v2"}
        )
        self.assertEqual(again["status"], "implemented")

    def test_reopen_after_commission_keeps_record(self):
        window = self.service.create(
            self.admin,
            "window",
            {
                "unit_id": self.unit["id"],
                "start": "2026-10-10T08:00:00",
                "end": "2026-10-10T20:00:00",
                "capacity": 1,
            },
        )
        change, items = self._implement_change(window)
        commissioned = self.service.transition(
            self.admin, change["id"], "commission", {"tests_passed": True}
        )
        original_level = commissioned["data"].get("risk_level")
        self.service.transition(
            self.admin, items[0]["id"], "reopen", {"reason": "field finding"}
        )
        kept = self.repo.get_entity(change["id"])
        self.assertEqual(kept["status"], "commissioned")
        self.assertEqual(kept["data"].get("risk_level"), original_level)


class RiskReassessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.service, self.repo = make_service(self.db)
        self.admin = Actor("admin", "admin")
        self.unit = self.service.create(
            self.admin, "unit", {"name": "U-1", "location": "Plant-A"}
        )
        self.change = self.service.create(
            self.admin,
            "change",
            {"unit_id": self.unit["id"], "description": "c"},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _items_by_measure(self):
        return {
            item["data"].get("measure"): item
            for item in self.repo.find_entities("action_item", "change_id", self.change["id"])
        }

    def test_level_change_invalidates_and_recomputes_measures(self):
        self.service.transition(
            self.admin,
            self.change["id"],
            "assess",
            {"risk_level": "medium", "analyst": "A"},
        )
        items = self._items_by_measure()
        permit = items["work_permit"]
        briefing = items["site_briefing"]
        # 复核通过一项，另一项只完成未复核
        self.service.transition(
            self.admin, permit["id"], "complete", {"completed_by": "O", "evidence": "e"}
        )
        self.service.transition(
            self.admin, permit["id"], "verify", {"verifier": "V"}
        )
        self.service.transition(
            self.admin,
            briefing["id"],
            "complete",
            {"completed_by": "O", "evidence": "e"},
        )

        updated = self.service.transition(
            self.admin,
            self.change["id"],
            "reassess",
            {"risk_level": "high", "analyst": "A2", "reason": "new HAZOP note"},
        )
        self.assertEqual(updated["status"], "assessed")
        self.assertEqual(updated["data"]["required_approvals"], 3)
        self.assertEqual(
            sorted(updated["data"]["required_measures"]),
            ["dual_verification", "site_briefing", "work_permit"],
        )
        # 未复核的结论作废
        briefing_now = self.repo.get_entity(briefing["id"])
        self.assertEqual(briefing_now["status"], "open")
        self.assertIn("invalidated_conclusion", briefing_now["data"])
        # 已复核的结论保留
        self.assertEqual(self.repo.get_entity(permit["id"])["status"], "verified")
        # 新措施补开
        self.assertIn("dual_verification", self._items_by_measure())

    def test_downgrade_cancels_obsolete_unverified_keeps_verified(self):
        self.service.transition(
            self.admin,
            self.change["id"],
            "assess",
            {"risk_level": "high", "analyst": "A"},
        )
        items = self._items_by_measure()
        briefing = items["site_briefing"]
        permit = items["work_permit"]
        dual = items["dual_verification"]
        # 一项已复核，一项完成未复核，一项未动；降级 high -> medium
        for item in (briefing, permit):
            self.service.transition(
                self.admin,
                item["id"],
                "complete",
                {"completed_by": "O", "evidence": "e"},
            )
        self.service.transition(
            self.admin, briefing["id"], "verify", {"verifier": "V"}
        )
        self.service.transition(
            self.admin,
            self.change["id"],
            "reassess",
            {"risk_level": "medium", "analyst": "A", "reason": "scope reduced"},
        )
        # 已复核的旧措施记录保留；即使 medium 不再要求，也不动
        self.assertEqual(self.repo.get_entity(briefing["id"])["status"], "verified")
        # 不再要求且未复核的措施作废
        self.assertEqual(self.repo.get_entity(dual["id"])["status"], "cancelled")
        # 仍要求但未复核的结论作废，回到 open 重新复核
        permit_now = self.repo.get_entity(permit["id"])
        self.assertEqual(permit_now["status"], "open")
        self.assertIn("invalidated_conclusion", permit_now["data"])

    def test_commissioned_change_keeps_original_record(self):
        window = self.service.create(
            self.admin,
            "window",
            {
                "unit_id": self.unit["id"],
                "start": "2026-10-10T08:00:00",
                "end": "2026-10-10T20:00:00",
                "capacity": 1,
            },
        )
        self.service.transition(
            self.admin,
            self.change["id"],
            "assess",
            {"risk_level": "low", "analyst": "A"},
        )
        for item in self.repo.find_entities("action_item", "change_id", self.change["id"]):
            self.service.transition(
                self.admin,
                item["id"],
                "complete",
                {"completed_by": "O", "evidence": "e"},
            )
            self.service.transition(
                self.admin, item["id"], "verify", {"verifier": "V"}
            )
        self.service.transition(
            self.admin,
            self.change["id"],
            "approve",
            {"approvals": ["S-1"], "permit_id": "P"},
        )
        self.service.submit_window(
            self.admin, window["id"], {"change_id": self.change["id"]}, "k"
        )
        self.service.transition(
            self.admin, self.change["id"], "implement", {"procedure_version": "v1"}
        )
        self.service.transition(
            self.admin, self.change["id"], "commission", {"tests_passed": True}
        )
        before = self.repo.get_entity(self.change["id"])
        updated = self.service.transition(
            self.admin,
            self.change["id"],
            "reassess",
            {"risk_level": "critical", "analyst": "A2", "reason": "post-op review"},
        )
        self.assertEqual(updated["status"], "commissioned")
        self.assertEqual(updated["data"]["risk_level"], "low")
        self.assertEqual(updated["data"]["required_approvals"], before["data"]["required_approvals"])
        self.assertEqual(updated["version"], before["version"] + 1)
        self.assertEqual(updated["data"]["risk_reviews"][-1]["risk_level"], "critical")
        self.assertTrue(updated["data"]["risk_reviews"][-1]["append_only"])


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "old.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_data_backfilled_and_still_opens(self):
        now = "2025-01-01T00:00:00+00:00"
        connection = sqlite3.connect(self.db)
        connection.executescript(
            """
            CREATE TABLE entities (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                version INTEGER NOT NULL, data TEXT NOT NULL,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id TEXT NOT NULL,
                actor_id TEXT NOT NULL, actor_role TEXT NOT NULL, action TEXT NOT NULL,
                from_status TEXT, to_status TEXT NOT NULL, detail TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE idempotency (
                actor_id TEXT NOT NULL, idem_key TEXT NOT NULL, entity_id TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(actor_id, idem_key)
            );
            """
        )

        def insert(entity_id, kind, status, data):
            connection.execute(
                "INSERT INTO entities VALUES (?,?,?,1,?,?,?,?)",
                (
                    entity_id,
                    kind,
                    status,
                    json.dumps(data, ensure_ascii=False, sort_keys=True),
                    "legacy",
                    now,
                    now,
                ),
            )

        insert("ch-ready", "change", "approved", {"unit_id": "u1", "description": "ready"})
        insert("ch-pending", "change", "approved", {"unit_id": "u1", "description": "pending"})
        insert("ch-done", "change", "implemented", {"unit_id": "u1", "description": "done", "risk_level": "low"})
        insert("ch-draft", "change", "draft", {"unit_id": "u1", "description": "draft"})
        insert(
            "ai-verified",
            "action_item",
            "verified",
            {"change_id": "ch-ready", "description": "a", "owner": "o"},
        )
        insert(
            "ai-open",
            "action_item",
            "open",
            {"change_id": "ch-pending", "description": "b", "owner": "o"},
        )
        connection.commit()
        connection.close()

        # 打开即升级
        repo = SQLiteRepository(self.db)
        ready = repo.get_entity("ch-ready")
        self.assertEqual(ready["data"]["implementation_state"], "ready")
        self.assertEqual(repo.get_entity("ch-pending")["data"]["implementation_state"], "pending")
        self.assertEqual(repo.get_entity("ch-done")["data"]["implementation_state"], "fulfilled")
        self.assertEqual(repo.get_entity("ch-draft")["data"]["implementation_state"], "pending")
        self.assertEqual(
            repo.get_entity("ch-done")["data"]["required_measures"], ["site_briefing"]
        )
        self.assertEqual(
            repo.get_entity("ai-verified")["data"]["review"],
            {"result": "verified", "source": "backfill"},
        )
        self.assertEqual(
            repo.get_entity("ai-open")["data"]["review"]["result"], "outstanding"
        )
        # 历史变更仍能打开并继续操作
        service = DomainService(repo, RuleEngine())
        self.assertEqual(service.get("ch-ready")["status"], "approved")
        actions = [row["action"] for row in repo.list_audit("ch-ready")]
        self.assertIn("migration_backfill", actions)
        # 再次打开不重复回填
        repo2 = SQLiteRepository(self.db)
        self.assertEqual(
            [r["action"] for r in repo2.list_audit("ch-ready")].count("migration_backfill"),
            1,
        )


if __name__ == "__main__":
    unittest.main()
