import unittest

from src.rules import required_approval_level, required_measures
from src.domain import Actor, PermissionDenied, ValidationError
from src.rules import RuleEngine


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.admin = Actor("rule-tester", "admin")

    def test_rule_calculation_or_validation(self):
        self.assertEqual(required_approval_level("low"), 1)
        self.assertEqual(required_approval_level("high"), 3)
        self.assertEqual(required_approval_level("unknown"), 4)
        self.assertEqual(required_measures("critical")[-1], "special_plan")
        with self.assertRaises(ValidationError):
            self.rules.validate_transition(self.admin, {"kind": "change", "status": "assessed", "id": "c1", "data": {"required_approvals": 2}}, "approve", {"approvals": ["one"], "permit_id": "p"})

    def test_implementation_requires_verified_items_and_scheduling(self):
        lookup = {
            "action_item": [
                {"id": "a1", "status": "open", "data": {}},
                {"id": "a2", "status": "verified", "data": {}},
            ]
        }

        def find(kind, field, value):
            return lookup.get(kind, []) if field == "change_id" else []

        with self.assertRaises(ValidationError):
            self.rules.validate_transition(
                self.admin,
                {"kind": "change", "status": "approved", "id": "c1", "data": {}},
                "implement",
                {"procedure_version": "v1"},
                find,
            )


if __name__ == "__main__":
    unittest.main()
