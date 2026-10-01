import copy
import unittest

from src.domain import ValidationError
from src.rules import CASE_TYPES, DomainRules, SEED_POLICY


CREATE_DATA = {'applicant_id': 'A-900', 'case_type': 'family', 'received_day': 100, 'response_day': 110, 'representation_active': True, 'required_documents': ['passport', 'sponsor_letter']}
FLOW = [('submit', 'legal_rep', {'documents': ['passport', 'sponsor_letter']}, 'submitted'), ('request_evidence', 'case_officer', {'evidence_request_day': 115, 'allowed_days': 10, 'evidence_request': '补充收入证明'}, 'evidence_requested'), ('respond', 'legal_rep', {'response_day': 120, 'documents': ['income_proof']}, 'response_received'), ('decide', 'case_officer', {'decision': 'granted', 'decision_reason': '材料充分'}, 'decided')]


def seed_policy(version=1):
    return {'version': version, 'content': copy.deepcopy(SEED_POLICY)}


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_prepare_create_snapshots_policy(self):
        prepared = self.rules.prepare_create(CREATE_DATA, seed_policy())
        self.assertEqual(prepared['deadline_day'], 130)
        self.assertEqual(prepared['days_remaining'], 20)
        self.assertFalse(prepared['overdue'])
        # 受理时把当时的政策整体留给案件，必备材料以政策为准（忽略请求里的清单）。
        self.assertEqual(prepared['policy']['version'], 1)
        self.assertEqual(prepared['required_documents'], ['passport', 'sponsor_letter'])

    def test_evidence_days_follow_snapshot_policy(self):
        # 即便当前政策变了，补件期限也只认案件快照里的原版本。
        changed = seed_policy()
        changed['content']['evidence_days']['family'] = 10
        record = {'id': 1, 'state': 'submitted', 'payload': self.rules.prepare_create(CREATE_DATA, seed_policy())}
        record['state'] = 'submitted'
        record['payload']['submitted_documents'] = ['passport', 'sponsor_letter']
        _, payload, _ = self.rules.apply_action(record, 'request_evidence', {'evidence_request_day': 115, 'evidence_request': '补收入证明'})
        self.assertEqual(payload['allowed_days'], 10)
        self.assertEqual(payload['evidence_due_day'], 125)
        self.assertEqual(payload['evidence_policy_version'], 1)

    def test_draft_rebase_uses_new_version(self):
        prepared = self.rules.prepare_create(CREATE_DATA, seed_policy())
        # 草稿阶段已交过部分材料，重算时保留已交项、只对新增材料报缺。
        prepared['submitted_documents'] = ['passport']
        new_content = copy.deepcopy(SEED_POLICY)
        new_content['deadline_days']['family'] = 45
        new_content['required_documents']['family'] = ['passport', 'sponsor_letter', 'tax_form']
        rebased = self.rules.rebase_draft(prepared, 2, new_content)
        self.assertEqual(rebased['policy']['version'], 2)
        self.assertEqual(rebased['deadline_day'], 145)
        self.assertEqual(rebased['days_remaining'], 35)
        self.assertEqual(rebased['required_documents'], ['passport', 'sponsor_letter', 'tax_form'])
        self.assertEqual(rebased['missing_documents'], ['sponsor_letter', 'tax_form'])

    def test_policy_content_validation(self):
        bad = copy.deepcopy(SEED_POLICY)
        bad['deadline_days']['family'] = 0
        with self.assertRaises(ValidationError):
            self.rules.validate_policy_content(bad)
        bad2 = copy.deepcopy(SEED_POLICY)
        del bad2['evidence_days']['work']
        with self.assertRaises(ValidationError):
            self.rules.validate_policy_content(bad2)

    def test_action_calculation(self):
        action, role, data, expected_state = FLOW[0]
        record = {"id": 1, "state": self.rules.INITIAL_STATE, "payload": self.rules.prepare_create(CREATE_DATA, seed_policy())}
        state, payload, summary = self.rules.apply_action(record, action, data)
        self.assertEqual(state, expected_state)
        self.assertEqual(payload["missing_documents"], [])
        self.assertIn("v1", summary)

    def test_invalid_input(self):
        invalid = dict(CREATE_DATA)
        invalid["case_type"] = 'tourist'
        with self.assertRaises(ValidationError):
            self.rules.prepare_create(invalid, seed_policy())
