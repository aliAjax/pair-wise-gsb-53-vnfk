import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, PermissionDenied


CREATE_DATA = {'applicant_id': 'A-900', 'case_type': 'family', 'received_day': 100, 'response_day': 110, 'representation_active': True}
ORG_A_INTAKE = Actor("clerk-a", "intake_officer", "org-a")
ORG_A_REP = Actor("rep-a", "legal_rep", "org-a")
ORG_B_REP = Actor("rep-b", "legal_rep", "org-b")
ORG_B_ADMIN = Actor("admin-b", "admin", "org-b")


class OrgAccessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.record = self.service.create(ORG_A_INTAKE, "IMM-100", dict(CREATE_DATA), expected_policy_version=1)

    def tearDown(self):
        self.temp.cleanup()

    def test_cross_org_action_rejected(self):
        with self.assertRaises(PermissionDenied):
            self.service.act(ORG_B_REP, self.record["id"], 1, "submit", {"documents": ["passport", "sponsor_letter"]})

    def test_cross_org_read_and_timeline_rejected(self):
        with self.assertRaises(PermissionDenied):
            self.service.get_record(ORG_B_REP, self.record["id"])
        with self.assertRaises(PermissionDenied):
            self.service.timeline(ORG_B_REP, self.record["id"])

    def test_admin_from_other_org_also_rejected(self):
        with self.assertRaises(PermissionDenied):
            self.service.act(ORG_B_ADMIN, self.record["id"], 1, "submit", {"documents": ["passport", "sponsor_letter"]})

    def test_list_and_stats_scoped_by_org(self):
        self.assertEqual(len(self.service.list_records(ORG_A_INTAKE)), 1)
        self.assertEqual(self.service.list_records(ORG_B_REP), [])
        self.assertEqual(self.service.stats(ORG_A_INTAKE), {"draft": 1})
        self.assertEqual(self.service.stats(ORG_B_REP), {})

    def test_same_org_flow_works(self):
        updated = self.service.act(ORG_A_REP, self.record["id"], 1, "submit", {"documents": ["passport", "sponsor_letter"]})
        self.assertEqual(updated["state"], "submitted")

    def test_policy_is_global_across_orgs(self):
        current = self.service.current_policy(ORG_B_REP)
        self.assertEqual(current["version"], 1)
