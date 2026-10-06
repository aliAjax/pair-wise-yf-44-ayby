from datetime import datetime
from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    Actor,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .repository import SYSTEM_ACTOR
from .rules import MEASURE_NAMES, RuleEngine

SYSTEM = Actor(SYSTEM_ACTOR, "admin")
WINDOW_ROLES = ("admin", "engineer")
WINDOW_ADMIN_ROLES = ("admin", "engineer", "safety")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        if kind == "booking":
            raise ValidationError("bookings are created via POST /api/windows/<id>/bookings")
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    # -------------------------------------------------------------- scheduling
    def submit_window(self, actor, window_id, data=None, idem_key=None):
        if actor.role not in WINDOW_ROLES:
            raise PermissionDenied("role %s cannot book windows" % actor.role)
        data = dict(data or {})
        window = self.repository.get_entity(window_id)
        if not window or window["kind"] != "window":
            raise NotFoundError("window not found: " + window_id)
        if window["status"] != "open":
            raise ConflictError("window is not open: " + window_id)
        change_id = data.get("change_id")
        if not change_id:
            raise ValidationError("missing required field: change_id")
        change = self.repository.get_entity(change_id)
        if not change or change["kind"] != "change":
            raise ValidationError("change does not exist: " + str(change_id))
        if change["data"].get("unit_id") != window["data"].get("unit_id"):
            raise ValidationError("change and window do not belong to the same unit")
        if change["status"] in ("implemented", "commissioned", "rolled_back", "closed"):
            raise ConflictError("change %s already left the approval stage" % change_id)

        # 重试不占位：同一窗口+幂等键直接返回原排队/占位记录
        if idem_key:
            existing_id = self.repository.get_ledger_idem(window_id, idem_key)
            if existing_id:
                return self.repository.get_entity(existing_id), "existing"

        # 同一变更同一窗口只能有一条活记录
        for booking in self.repository.get_window_schedule(window_id):
            if (
                booking["data"].get("change_id") == change_id
                and booking["status"] in ("queued", "scheduled", "consumed")
            ):
                raise ConflictError("change %s already queued/scheduled in this window" % change_id)

        win_start = datetime.fromisoformat(window["data"]["start"])
        win_end = datetime.fromisoformat(window["data"]["end"])
        seg_start = (
            datetime.fromisoformat(data["segment_start"])
            if data.get("segment_start")
            else win_start
        )
        seg_end = (
            datetime.fromisoformat(data["segment_end"])
            if data.get("segment_end")
            else win_end
        )
        if not (win_start <= seg_start < seg_end <= win_end):
            raise ValidationError("segment must lie within the window: [start, end)")

        fit_before = self.repository.window_has_room(window_id, seg_start, seg_end)
        booking, state = self.repository.submit_booking(
            window_id,
            change_id,
            seg_start,
            seg_end,
            actor.user_id,
            idem_key,
            fit_before,
        )
        if state != "existing":
            self.audit.record(
                booking["id"],
                actor,
                "submit_" + state,
                None,
                state,
                {"window_id": window_id, "seq": booking["data"]["seq"]},
            )
        return booking, state

    def release_booking(self, actor, booking_id, reason=None):
        if actor.role not in WINDOW_ADMIN_ROLES:
            raise PermissionDenied("role %s cannot release bookings" % actor.role)
        booking = self.repository.get_entity(booking_id)
        if not booking or booking["kind"] != "booking":
            raise NotFoundError("booking not found: " + booking_id)
        from_status = booking["status"]
        released, promoted = self.repository.release_booking(booking_id, reason)
        self.audit.record(
            booking_id,
            actor,
            "release",
            from_status,
            "released",
            {"reason": reason or ""},
        )
        for entity in promoted:
            self.audit.record(
                entity["id"],
                SYSTEM,
                "auto_promote",
                "queued",
                "scheduled",
                {"window_id": entity["data"]["window_id"], "seq": entity["data"]["seq"]},
            )
        return released, promoted

    def recover_window(self, actor, window_id):
        if actor.role not in WINDOW_ADMIN_ROLES:
            raise PermissionDenied("role %s cannot recover schedule" % actor.role)
        window = self.repository.get_entity(window_id)
        if not window or window["kind"] != "window":
            raise NotFoundError("window not found: " + window_id)
        promoted = self.repository.recover_window(window_id)
        for entity in promoted:
            self.audit.record(
                entity["id"],
                actor,
                "recover_promote",
                "queued",
                "scheduled",
                {"window_id": window_id, "seq": entity["data"]["seq"]},
            )
        return promoted

    def window_schedule(self, window_id):
        window = self.repository.get_entity(window_id)
        if not window or window["kind"] != "window":
            raise NotFoundError("window not found: " + window_id)
        return self.repository.get_window_schedule(window_id)

    # ---------------------------------------------------------------- transition
    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        data = dict(data or {})

        if entity["kind"] == "change" and action == "reassess":
            return self._reassess(actor, entity, data, expected_version)

        next_status, patch = self.rules.validate_transition(
            actor, entity, action, data, self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)

        if entity["kind"] == "change" and action == "assess":
            merged.setdefault("risk_history", []).append(
                {
                    "risk_level": merged["risk_level"],
                    "analyst": merged.get("analyst"),
                    "at": entity["updated_at"],
                }
            )
            self._reconcile_measures(entity["id"], merged["required_measures"])

        if entity["kind"] == "change" and action == "implement":
            # 实施前必须有已占位的停车窗口
            booking = self.repository.get_scheduled_booking(entity_id)
            if not booking:
                raise ValidationError("change has no scheduled window slot; join a window first")
            merged["implementation_state"] = "fulfilled"

        updated = self.repository.update_entity(
            entity_id,
            int(expected_version) if expected_version is not None else entity["version"],
            next_status,
            merged,
        )
        self.audit.record(
            entity_id, actor, action, entity["status"], updated["status"], {"patch": patch}
        )

        if entity["kind"] == "change" and action == "implement":
            consumed = self.repository.consume_booking_for_change(entity_id)
            for booking_id in consumed:
                self.audit.record(
                    booking_id,
                    actor,
                    "consume",
                    "scheduled",
                    "consumed",
                    {"change_id": entity_id},
                )
        if entity["kind"] == "change" and action in ("commission", "rollback"):
            # 变更离开停车窗口：释放占用，队列按申请顺序恢复
            _, promoted = self.repository.release_bookings_for_change(entity_id)
            for item in promoted:
                self.audit.record(
                    item["id"],
                    SYSTEM,
                    "auto_promote",
                    "queued",
                    "scheduled",
                    {"window_id": item["data"]["window_id"], "seq": item["data"]["seq"]},
                )

        if entity["kind"] == "action_item" and action == "reopen":
            self._reopen_cascade(actor, entity, data)

        return updated

    def _reopen_cascade(self, actor, item, data):
        change = self.repository.get_entity(item["data"]["change_id"])
        if not change or change["kind"] != "change":
            return
        if change["status"] == "implemented":
            # 任一行动项重开：已开始的实施退回已批准，重新评估
            merged = dict(change["data"])
            merged["evaluation_reset"] = {
                "reason": data.get("reason"),
                "reopened_item": item["id"],
                "at": item["updated_at"],
            }
            updated = self.repository.update_entity_raw(change["id"], "approved", merged)
            self.audit.record(
                change["id"],
                actor,
                "auto_revert_to_approved",
                "implemented",
                "approved",
                {"reopened_item": item["id"], "reason": data.get("reason")},
            )
            return updated
        # commissioned 及以后的变更：原记录保留，不动状态
        return change

    def _reassess(self, actor, change, data, expected_version):
        # 先跑通用校验（状态、角色、必填、风险计算）
        next_status, patch = self.rules.validate_transition(
            actor, change, "reassess", data, self._lookup
        )
        entry = {
            "risk_level": patch["risk_level"],
            "required_measures": patch["required_measures"],
            "required_approvals": patch["required_approvals"],
            "analyst": patch.get("analyst"),
            "reason": patch.get("reason"),
            "at": change["updated_at"],
            "append_only": False,
        }

        if change["status"] == "commissioned":
            # 已投产：保留原记录，风险评审只追加不改写
            reviews = list(change["data"].get("risk_reviews", []))
            entry["append_only"] = True
            reviews.append(entry)
            updated = self.repository.update_entity_raw(change["id"], change["status"], {
                **change["data"],
                "risk_reviews": reviews,
            })
            self.audit.record(
                change["id"],
                actor,
                "reassess_append",
                change["status"],
                change["status"],
                {"risk_level": entry["risk_level"]},
            )
            return updated

        merged = dict(change["data"])
        merged.update(patch)
        merged.setdefault("risk_history", []).append(entry)
        # 未复核的结论作废并重算措施
        self._invalidate_unreviewed(change["id"], patch["risk_level"])
        self._reconcile_measures(change["id"], patch["required_measures"])
        updated = self.repository.update_entity(
            change["id"],
            int(expected_version) if expected_version is not None else change["version"],
            next_status,
            merged,
        )
        self.audit.record(
            change["id"],
            actor,
            "reassess",
            change["status"],
            next_status,
            {"risk_level": patch["risk_level"], "reason": patch.get("reason")},
        )
        return updated

    def _invalidate_unreviewed(self, change_id, risk_level):
        items = self._lookup("action_item", "change_id", change_id)
        for item in items:
            if item["status"] in ("verified", "cancelled"):
                continue
            data = dict(item["data"])
            data["invalidated_conclusion"] = {
                "former_status": item["status"],
                "new_risk_level": risk_level,
            }
            self.repository.update_entity_raw(item["id"], "open", data)
            self.audit.record(
                item["id"],
                SYSTEM,
                "invalidate_conclusion",
                item["status"],
                "open",
                {"new_risk_level": risk_level},
            )

    def _reconcile_measures(self, change_id, required_codes):
        """按风险等级重算措施：已复核的保留，缺项补开，不再要求且未复核的作废。"""
        items = self._lookup("action_item", "change_id", change_id)
        present = {}
        for item in items:
            code = item["data"].get("measure")
            if not code:
                continue
            if code not in required_codes and item["status"] != "verified":
                data = dict(item["data"])
                data["voided_reason"] = "measure-not-required"
                self.repository.update_entity_raw(item["id"], "cancelled", data)
                self.audit.record(
                    item["id"],
                    SYSTEM,
                    "cancel_obsolete_measure",
                    item["status"],
                    "cancelled",
                    {"measure": code},
                )
            else:
                present[code] = item
        for code in required_codes:
            if code in present:
                continue
            data = {
                "change_id": change_id,
                "description": MEASURE_NAMES.get(code, code),
                "owner": "safety-team",
                "measure": code,
                "derived_from": "risk_assessment",
            }
            item = self.repository.create_entity(
                str(uuid4()), "action_item", "open", data, SYSTEM_ACTOR
            )
            self.audit.record(item["id"], SYSTEM, "open_measure", None, "open", {"measure": code})

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
