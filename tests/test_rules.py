import unittest

from src.domain import Actor, ValidationError
from src.rules import DEFAULT_POLICY_RULES, DomainRules


CREATE_DATA = {'applicant_id': 'A-900', 'case_type': 'family', 'received_day': 100, 'response_day': 110, 'representation_active': True}
POLICY_V1 = {"version": 1, "name": "初始政策", "rules": DEFAULT_POLICY_RULES}
FLOW = [('submit', 'legal_rep', {'documents': ['passport', 'sponsor_letter']}, 'submitted'), ('request_evidence', 'case_officer', {'evidence_request_day': 115, 'allowed_days': 10, 'evidence_request': '补充收入证明'}, 'evidence_requested'), ('respond', 'legal_rep', {'response_day': 120, 'documents': ['income_proof']}, 'response_received'), ('decide', 'case_officer', {'decision': 'granted', 'decision_reason': '材料充分'}, 'decided')]


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_prepare_create_snapshots_policy(self):
        prepared = self.rules.prepare_create(CREATE_DATA, POLICY_V1)
        self.assertEqual(prepared["deadline_day"], 130)
        self.assertEqual(prepared["days_remaining"], 20)
        self.assertFalse(prepared["overdue"])
        self.assertEqual(prepared["policy_version"], 1)
        self.assertEqual(prepared["policy_snapshot"]["deadline_days"], 30)
        self.assertEqual(prepared["policy_snapshot"]["evidence_allowed_days"], 10)
        self.assertEqual(prepared["required_documents"], ['passport', 'sponsor_letter'])

    def test_action_calculation(self):
        action, role, data, expected_state = FLOW[0]
        record = {"id": 1, "state": self.rules.INITIAL_STATE, "payload": self.rules.prepare_create(CREATE_DATA, POLICY_V1)}
        state, payload, summary = self.rules.apply_action(record, action, data)
        self.assertEqual(state, expected_state)
        self.assertEqual(payload["missing_documents"], [])

    def test_evidence_days_default_from_snapshot(self):
        record = {"id": 1, "state": "submitted", "payload": self.rules.prepare_create(CREATE_DATA, POLICY_V1)}
        state, payload, summary = self.rules.apply_action(record, "request_evidence", {'evidence_request_day': 115, 'evidence_request': '补充收入证明'})
        self.assertEqual(payload["evidence_due_day"], 125)

    def test_appeal_window_from_snapshot(self):
        record = {"id": 1, "state": "decided", "payload": self.rules.prepare_create(CREATE_DATA, POLICY_V1)}
        state, payload, summary = self.rules.apply_action(record, "appeal", {'appeal_day': 160, 'appeal_reason': '程序错误'})
        self.assertEqual(payload["appeal_day"], 160)
        with self.assertRaises(ValidationError):
            self.rules.apply_action(record, "appeal", {'appeal_day': 161, 'appeal_reason': '程序错误'})

    def test_recalculate_payload_uses_new_policy(self):
        prepared = self.rules.prepare_create(CREATE_DATA, POLICY_V1)
        new_rules = {case_type: dict(section) for case_type, section in DEFAULT_POLICY_RULES.items()}
        new_rules["family"]["deadline_days"] = 45
        recalced = self.rules.recalculate_payload(prepared, {"version": 2, "name": "新政策", "rules": new_rules})
        self.assertEqual(recalced["policy_version"], 2)
        self.assertEqual(recalced["deadline_day"], 145)
        self.assertEqual(recalced["days_remaining"], 35)

    def test_validate_policy(self):
        cleaned = self.rules.validate_policy({"name": "v2", "rules": DEFAULT_POLICY_RULES})
        self.assertEqual(cleaned["name"], "v2")
        with self.assertRaises(ValidationError):
            bad = {"name": "v2", "rules": {k: v for k, v in DEFAULT_POLICY_RULES.items() if k != "work"}}
            self.rules.validate_policy(bad)
        with self.assertRaises(ValidationError):
            bad = {"name": "v2", "rules": dict(DEFAULT_POLICY_RULES, tourist={"deadline_days": 1, "evidence_allowed_days": 1, "appeal_window_days": 1, "required_documents": ["x"]})}
            self.rules.validate_policy(bad)

    def test_invalid_input(self):
        invalid = dict(CREATE_DATA)
        invalid["case_type"] = 'tourist'
        with self.assertRaises(ValidationError):
            self.rules.prepare_create(invalid, POLICY_V1)
