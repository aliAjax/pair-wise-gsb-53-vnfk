import copy
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError
from src.rules import DEFAULT_POLICY_RULES


CREATE_DATA = {'applicant_id': 'A-900', 'case_type': 'family', 'received_day': 100, 'response_day': 110, 'representation_active': True}
ADMIN = Actor("admin-1", "admin")
INTAKE = Actor("clerk-1", "intake_officer")


def policy_payload(name, **family_overrides):
    rules = copy.deepcopy(DEFAULT_POLICY_RULES)
    rules["family"].update(family_overrides)
    return {"name": name, "rules": rules}


class PolicyVersionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def create_case(self, reference="IMM-100", data=None, version=1):
        return self.service.create(INTAKE, reference, data or dict(CREATE_DATA), expected_policy_version=version)

    def test_seed_policy_exists(self):
        current = self.service.current_policy(INTAKE)
        self.assertEqual(current["version"], 1)
        self.assertEqual(current["created_by"], "system")

    def test_intake_snapshots_current_policy(self):
        record = self.create_case()
        self.assertEqual(record["payload"]["policy_version"], 1)
        self.assertEqual(record["payload"]["policy_snapshot"]["deadline_days"], 30)
        self.assertEqual(record["payload"]["deadline_day"], 130)

    def test_draft_recalculated_on_publish(self):
        record = self.create_case()
        result = self.service.publish_policy(ADMIN, 1, policy_payload("v2", deadline_days=45))
        self.assertEqual(result["policy"]["version"], 2)
        self.assertEqual(result["recalculated_drafts"], 1)
        updated = self.service.get_record(INTAKE, record["id"])
        self.assertEqual(updated["payload"]["policy_version"], 2)
        self.assertEqual(updated["payload"]["deadline_day"], 145)
        self.assertEqual(updated["version"], record["version"] + 1)
        timeline = self.service.timeline(INTAKE, record["id"])
        self.assertEqual(timeline[-1]["action"], "policy_recalc")
        self.assertEqual(timeline[-1]["details"]["from_policy"], 1)
        self.assertEqual(timeline[-1]["details"]["to_policy"], 2)

    def test_submitted_case_pinned_to_original_version(self):
        record = self.create_case()
        record = self.service.act(Actor("rep", "legal_rep"), record["id"], record["version"], "submit", {"documents": ["passport", "sponsor_letter"]})
        self.service.publish_policy(ADMIN, 1, policy_payload("v2", deadline_days=45, evidence_allowed_days=60))
        pinned = self.service.get_record(INTAKE, record["id"])
        self.assertEqual(pinned["state"], "submitted")
        self.assertEqual(pinned["payload"]["policy_version"], 1)
        self.assertEqual(pinned["payload"]["deadline_day"], 130)
        self.assertEqual(pinned["version"], record["version"])

    def test_evidence_deadline_follows_original_version(self):
        record = self.create_case()
        record = self.service.act(Actor("rep", "legal_rep"), record["id"], record["version"], "submit", {"documents": ["passport", "sponsor_letter"]})
        self.service.publish_policy(ADMIN, 1, policy_payload("v2", evidence_allowed_days=60))
        record = self.service.get_record(INTAKE, record["id"])
        record = self.service.act(Actor("officer", "case_officer"), record["id"], record["version"], "request_evidence", {"evidence_request_day": 115, "evidence_request": "补充收入证明"})
        # 补件期限按钉住的v1（10天）而不是新发布的v2（60天）
        self.assertEqual(record["payload"]["evidence_allowed_days"], 10)
        self.assertEqual(record["payload"]["evidence_due_day"], 125)

    def test_rollback_only_creates_new_version(self):
        self.service.publish_policy(ADMIN, 1, policy_payload("v2", deadline_days=45))
        result = self.service.rollback_policy(ADMIN, 2, 1)
        self.assertEqual(result["policy"]["version"], 3)
        self.assertEqual(result["policy"]["source_version"], 1)
        self.assertEqual(result["policy"]["rules"]["family"]["deadline_days"], 30)
        versions = [p["version"] for p in self.service.list_policies(ADMIN)]
        self.assertEqual(versions, [3, 2, 1])
        current = self.service.current_policy(ADMIN)
        self.assertEqual(current["version"], 3)
        # 历史版本保持原样
        v1 = self.service.repository.get_policy_by_version(1)
        self.assertEqual(v1["name"], "初始政策（系统预置）")

    def test_rollback_recalculates_drafts(self):
        record = self.create_case()
        self.service.publish_policy(ADMIN, 1, policy_payload("v2", deadline_days=45))
        self.service.rollback_policy(ADMIN, 2, 1)
        updated = self.service.get_record(INTAKE, record["id"])
        self.assertEqual(updated["payload"]["policy_version"], 3)
        self.assertEqual(updated["payload"]["deadline_day"], 130)

    def test_rollback_target_must_be_older(self):
        self.service.publish_policy(ADMIN, 1, policy_payload("v2"))
        with self.assertRaises(ValidationError):
            self.service.rollback_policy(ADMIN, 2, 2)
        with self.assertRaises(Exception):
            self.service.rollback_policy(ADMIN, 2, 99)

    def test_publish_requires_admin_and_matching_version(self):
        with self.assertRaises(PermissionDenied):
            self.service.publish_policy(INTAKE, 1, policy_payload("v2"))
        self.service.publish_policy(ADMIN, 1, policy_payload("v2"))
        with self.assertRaises(Conflict):
            self.service.publish_policy(ADMIN, 1, policy_payload("v3"))

    def test_intake_with_stale_policy_version_rejected(self):
        self.service.publish_policy(ADMIN, 1, policy_payload("v2"))
        with self.assertRaises(Conflict):
            self.create_case(version=1)
        record = self.create_case(version=2)
        self.assertEqual(record["payload"]["policy_version"], 2)

    def test_invalid_policy_payload_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.publish_policy(ADMIN, 1, {"name": "v2", "rules": {"family": {}}})
        current = self.service.current_policy(ADMIN)
        self.assertEqual(current["version"], 1)
