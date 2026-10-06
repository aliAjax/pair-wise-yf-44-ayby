from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .repository import utcnow, utcnow_micro
from .rules import RuleEngine, required_approval_level


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _require_admin(self, actor):
        if actor.role != "admin":
            raise PermissionDenied("admin role required")

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
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
        if kind == "window":
            payload.setdefault("capacity", 1)
            payload["scheduled_count"] = 0
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if action == "schedule":
            return self.schedule_change(actor, entity_id, data or {})
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        if action == "assess" and entity["status"] != "draft":
            next_status = entity["status"]
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self._after_transition(actor, entity, updated, action, data or {})
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _after_transition(self, actor, before, after, action, data):
        # 风险等级一变，未复核的结论作废并重算措施；已投产的变更保留原记录。
        if action == "assess" and after["kind"] == "change":
            old_level = before["data"].get("risk_level")
            new_level = data.get("risk_level")
            if old_level and str(old_level).lower() != str(new_level).lower():
                if after["status"] != "commissioned":
                    self._recalc_measures(after, new_level, actor)
        # 任一项重开后，已开始的实施退回已批准并重新评估。
        if action == "reopen" and after["kind"] == "action_item":
            change = self.repository.get_entity(after["data"].get("change_id"))
            if change and change["status"] == "implemented":
                change_data = dict(change["data"])
                change_data["reeval_pending"] = True
                self.repository.update_entity(
                    change["id"], change["version"], "approved", change_data
                )
                self.audit.record(
                    change["id"],
                    actor,
                    "reopen",
                    "implemented",
                    "approved",
                    {"item_id": after["id"], "reason": data.get("reason")},
                )

    def _recalc_measures(self, change, new_level, actor):
        items = self._lookup("action_item", "change_id", change["id"])
        for item in items:
            if item["status"] != "verified":
                self.repository.delete_entity(item["id"])
        count = required_approval_level(new_level)
        for index in range(count):
            self.repository.create_entity(
                str(uuid4()),
                "action_item",
                "open",
                {
                    "change_id": change["id"],
                    "description": "风险控制措施 %d/%d（依据风险等级 %s）"
                    % (index + 1, count, new_level),
                    "owner": actor.user_id,
                },
                actor.user_id,
            )

    def schedule_change(self, actor, change_id, data):
        change = self.repository.get_entity(change_id)
        if not change:
            raise NotFoundError("entity not found: " + change_id)
        if change["kind"] != "change":
            raise ValidationError("only changes can be scheduled")
        if change["status"] != "approved":
            raise ValidationError("change must be approved before scheduling")
        window_id = data.get("window_id")
        if not window_id:
            raise ValidationError("window_id is required")
        idem_key = data.get("idempotency_key")
        if idem_key:
            existing = self.repository.get_idempotency(actor.user_id, idem_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        window = self.repository.get_entity(window_id)
        if not window or window["kind"] != "window":
            raise NotFoundError("window not found: " + window_id)
        if window["status"] != "open":
            raise ValidationError("window is not open for scheduling")
        # 同一变更已有生效排程：重试不占位，重复提交收到冲突。
        active = [
            sched
            for sched in self._lookup("schedule", "change_id", change_id)
            if sched["status"] in ("scheduled", "queued")
        ]
        if active:
            existing = active[0]
            if idem_key and existing["data"].get("window_id") == window_id:
                return existing
            raise ConflictError("change already scheduled: " + existing["id"])
        claimed = self.repository.claim_window_slot(window_id)
        if claimed is None:
            status = "queued"
        else:
            status = "scheduled"
        sched_id = str(uuid4())
        self.repository.create_entity(
            sched_id,
            "schedule",
            status,
            {
                "change_id": change_id,
                "window_id": window_id,
                "unit_id": change["data"]["unit_id"],
                "requested_at": utcnow_micro(),
                "requested_by": actor.user_id,
            },
            actor.user_id,
        )
        change_data = dict(change["data"])
        change_data["window_id"] = window_id
        change_data["schedule_status"] = status
        if status == "scheduled":
            change_data["scheduled_at"] = utcnow()
        self.repository.update_entity(change_id, change["version"], change["status"], change_data)
        self.audit.record(
            change_id,
            actor,
            "schedule",
            change["status"],
            change["status"],
            {"window_id": window_id, "schedule_status": status},
        )
        if idem_key:
            self.repository.save_idempotency(actor.user_id, idem_key, sched_id)
        return self.repository.get_entity(change_id)

    def recover_schedule(self, actor):
        """按申请顺序恢复排队的排程，重试不占位。"""
        self._require_admin(actor)
        queued = self.repository.list_entities(kind="schedule", status="queued")
        queued.sort(key=lambda s: s["data"].get("requested_at", ""))
        recovered = []
        for sched in queued:
            change_id = sched["data"]["change_id"]
            # 幂等：已占槽的排程不重复占位。
            already = [
                s
                for s in self._lookup("schedule", "change_id", change_id)
                if s["status"] == "scheduled"
            ]
            if already:
                continue
            window_id = sched["data"]["window_id"]
            if self.repository.claim_window_slot(window_id) is None:
                continue
            sched_data = dict(sched["data"])
            self.repository.update_entity(sched["id"], sched["version"], "scheduled", sched_data)
            change = self.repository.get_entity(change_id)
            if change:
                change_data = dict(change["data"])
                change_data["schedule_status"] = "scheduled"
                change_data["scheduled_at"] = utcnow()
                self.repository.update_entity(
                    change["id"], change["version"], change["status"], change_data
                )
            recovered.append(sched["id"])
        return {"recovered": len(recovered), "items": recovered}

    def backfill(self, actor):
        """升级时按已有复核记录回填落实状态，历史变更仍能打开。"""
        self._require_admin(actor)
        changes = self.repository.list_entities(kind="change")
        count = 0
        for change in changes:
            if "implementation_status" in change["data"]:
                continue
            items = self._lookup("action_item", "change_id", change["id"])
            if items and all(item["status"] == "verified" for item in items):
                impl_status = "confirmed"
            else:
                impl_status = "pending"
            data = dict(change["data"])
            data["implementation_status"] = impl_status
            self.repository.update_entity(change["id"], change["version"], change["status"], data)
            count += 1
        return {"backfilled": count}

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
