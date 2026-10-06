from datetime import datetime

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


# 风险等级 -> 至少需要的独立审批人数
def required_approval_level(risk_level):
    levels = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    return levels.get(str(risk_level).lower(), 4)


# 风险等级 -> 必须落实的风险控制措施（重算措施的基准）
RISK_MEASURES = {
    "low": ["site_briefing"],
    "medium": ["site_briefing", "work_permit"],
    "high": ["site_briefing", "work_permit", "dual_verification"],
    "critical": [
        "site_briefing",
        "work_permit",
        "dual_verification",
        "escalated_approval",
        "special_plan",
    ],
}

MEASURE_NAMES = {
    "site_briefing": "现场交底",
    "work_permit": "作业许可",
    "dual_verification": "双人复核",
    "escalated_approval": "升级审批",
    "special_plan": "专项应急预案",
}


def required_measures(risk_level):
    return list(RISK_MEASURES.get(str(risk_level).lower(), RISK_MEASURES["critical"]))


def _parse_dt(value, field):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValidationError("invalid ISO datetime for %s: %s" % (field, value))


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _validate_change(actor, data, lookup):
    unit = _find_one(lookup, "unit", "id", data.get("unit_id"))
    if not unit:
        raise ValidationError("unit does not exist")
    if not data.get("description", "").strip():
        raise ValidationError("change description is required")


def _validate_window(actor, data, lookup):
    unit = _find_one(lookup, "unit", "id", data.get("unit_id"))
    if not unit:
        raise ValidationError("unit does not exist")
    start = _parse_dt(data.get("start"), "start")
    end = _parse_dt(data.get("end"), "end")
    if not start < end:
        raise ValidationError("window start must be before end")
    capacity = data.get("capacity")
    if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
        raise ValidationError("capacity must be a positive integer")


def _risk_patch(data):
    level = str(data.get("risk_level")).lower()
    return {
        "risk_level": level,
        "required_approvals": required_approval_level(level),
        "required_measures": required_measures(level),
    }


def _validate_assess(actor, entity, data, lookup):
    return _risk_patch(data)


def _validate_reassess(actor, entity, data, lookup):
    return _risk_patch(data)


def _validate_approve(actor, entity, data, lookup):
    required = int(entity["data"].get("required_approvals", 1))
    approvals = data.get("approvals") or []
    if len(set(approvals)) < required:
        raise ValidationError("not enough distinct approvals")
    return {"approved_by": actor.user_id}


def _unresolved_items(items):
    # cancelled 是作废措施，不再阻塞实施；其余必须全部 verified
    return [item["id"] for item in items if item["status"] not in ("verified", "cancelled")]


def _validate_implement(actor, entity, data, lookup):
    items = lookup("action_item", "change_id", entity["id"]) if lookup else []
    unresolved = _unresolved_items(items)
    if unresolved:
        raise ValidationError("action items not fully verified: " + ", ".join(unresolved))
    return {"implemented_by": actor.user_id}


def _validate_commission(actor, entity, data, lookup):
    items = lookup("action_item", "change_id", entity["id"]) if lookup else []
    unresolved = _unresolved_items(items)
    if unresolved:
        raise ValidationError("unresolved action items: " + ", ".join(unresolved))
    return {"commissioned_by": actor.user_id}


CUSTOM_CREATE = {"change": _validate_change, "window": _validate_window}
CUSTOM_TRANSITIONS = {
    ("change", "assess"): _validate_assess,
    ("change", "reassess"): _validate_reassess,
    ("change", "approve"): _validate_approve,
    ("change", "implement"): _validate_implement,
    ("change", "commission"): _validate_commission,
}


class RuleEngine:
    ALIASES = {
        "units": "unit",
        "changes": "change",
        "action_items": "action_item",
        "windows": "window",
        "bookings": "booking",
    }
    INITIAL_STATUS = {
        "unit": "operating",
        "change": "draft",
        "action_item": "open",
        "window": "open",
    }
    TRANSITIONS = {
        "unit": {
            "shutdown": (("operating",), "shutdown"),
            "startup": (("shutdown",), "operating"),
            "freeze": (("operating",), "frozen"),
            "unfreeze": (("frozen",), "operating"),
        },
        "change": {
            "assess": (("draft",), "assessed"),
            "reassess": (("assessed", "approved", "commissioned"), "assessed"),
            "approve": (("assessed",), "approved"),
            "implement": (("approved",), "implemented"),
            "commission": (("implemented",), "commissioned"),
            "rollback": (("implemented", "commissioned"), "rolled_back"),
            "close": (("rolled_back",), "closed"),
        },
        "action_item": {
            "complete": (("open",), "completed"),
            "verify": (("completed",), "verified"),
            "reopen": (("verified",), "open"),
        },
        "window": {"close": (("open",), "closed")},
    }
    CREATE_REQUIRED = {
        "unit": ("name", "location"),
        "change": ("unit_id", "description"),
        "action_item": ("change_id", "description", "owner"),
        "window": ("unit_id", "start", "end", "capacity"),
    }
    ACTION_REQUIRED = {
        ("unit", "shutdown"): ("reason",),
        ("unit", "freeze"): ("reason",),
        ("change", "assess"): ("risk_level", "analyst"),
        ("change", "reassess"): ("risk_level", "analyst", "reason"),
        ("change", "approve"): ("approvals", "permit_id"),
        ("change", "implement"): ("procedure_version",),
        ("change", "commission"): ("tests_passed",),
        ("change", "rollback"): ("reason",),
        ("change", "close"): ("outcome",),
        ("action_item", "complete"): ("completed_by", "evidence"),
        ("action_item", "verify"): ("verifier",),
        ("action_item", "reopen"): ("reason",),
        ("window", "close"): ("reason",),
    }
    CREATE_ROLES = {
        "unit": ("admin", "engineer"),
        "change": ("admin", "engineer"),
        "action_item": ("admin", "safety"),
        "window": ("admin", "engineer"),
    }
    ROLE_ACTIONS = {
        "shutdown": ("admin", "operator"),
        "startup": ("admin", "operator"),
        "freeze": ("admin", "operator"),
        "unfreeze": ("admin", "operator"),
        "assess": ("admin", "engineer"),
        "reassess": ("admin", "engineer", "safety"),
        "approve": ("admin", "safety"),
        "implement": ("admin", "engineer"),
        "commission": ("admin", "engineer"),
        "rollback": ("admin", "engineer"),
        "close": ("admin", "safety"),
        "complete": ("admin", "engineer"),
        "verify": ("admin", "verifier"),
        "reopen": ("admin", "verifier", "safety"),
        "close_window": ("admin", "operator"),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch
