import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict
from src.rules import DEFAULT_POLICY_RULES


CREATE_DATA = {'applicant_id': 'A-900', 'case_type': 'family', 'received_day': 100, 'response_day': 110, 'representation_active': True}
ADMIN = Actor("admin-1", "admin")
INTAKE = Actor("clerk-1", "intake_officer")


def policy_payload(name, **family_overrides):
    rules = copy.deepcopy(DEFAULT_POLICY_RULES)
    rules["family"].update(family_overrides)
    return {"name": name, "rules": rules}


class IdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def fail_next_idempotency_write(self):
        """让下一次幂等键写入抛错，模拟事务收尾时的存储故障。"""
        original = self.service.repository.store_idempotency
        state = {"failed": False}

        def flaky(*args, **kwargs):
            if not state["failed"]:
                state["failed"] = True
                raise sqlite3.OperationalError("simulated write failure")
            return original(*args, **kwargs)

        self.service.repository.store_idempotency = flaky

    def test_retry_with_same_key_does_not_duplicate(self):
        first = self.service.create(INTAKE, "IMM-100", dict(CREATE_DATA), expected_policy_version=1, idem_key="create-1")
        replay = self.service.create(INTAKE, "IMM-100", dict(CREATE_DATA), expected_policy_version=1, idem_key="create-1")
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(len(self.service.list_records(INTAKE)), 1)
        timeline = self.service.timeline(INTAKE, first["id"])
        self.assertEqual(len([e for e in timeline if e["action"] == "created"]), 1)

    def test_same_key_with_different_payload_conflicts(self):
        self.service.create(INTAKE, "IMM-100", dict(CREATE_DATA), expected_policy_version=1, idem_key="create-2")
        other = dict(CREATE_DATA, applicant_id="A-901")
        with self.assertRaises(Conflict):
            self.service.create(INTAKE, "IMM-101", other, expected_policy_version=1, idem_key="create-2")

    def test_action_replay_returns_first_result(self):
        record = self.service.create(INTAKE, "IMM-100", dict(CREATE_DATA), expected_policy_version=1)
        docs = {"documents": ["passport", "sponsor_letter"]}
        submitted = self.service.act(Actor("rep", "legal_rep"), record["id"], 1, "submit", docs, idem_key="submit-1")
        replay = self.service.act(Actor("rep", "legal_rep"), record["id"], 1, "submit", docs, idem_key="submit-1")
        self.assertEqual(replay["version"], submitted["version"])
        submits = [e for e in self.service.timeline(INTAKE, record["id"]) if e["action"] == "submit"]
        self.assertEqual(len(submits), 1)

    def test_failed_create_rolls_back_and_retry_succeeds_once(self):
        self.fail_next_idempotency_write()
        with self.assertRaises(sqlite3.OperationalError):
            self.service.create(INTAKE, "IMM-100", dict(CREATE_DATA), expected_policy_version=1, idem_key="create-3")
        # 失败不留痕迹：无案件、无幂等记录
        self.assertEqual(self.service.list_records(INTAKE), [])
        self.assertIsNone(self.service.repository.find_idempotency(None, INTAKE.user_id, "create-3"))
        record = self.service.create(INTAKE, "IMM-100", dict(CREATE_DATA), expected_policy_version=1, idem_key="create-3")
        self.assertEqual(record["reference"], "IMM-100")
        self.assertEqual(len(self.service.list_records(INTAKE)), 1)

    def test_failed_publish_keeps_old_snapshots_and_retry_is_clean(self):
        draft = self.service.create(INTAKE, "IMM-100", dict(CREATE_DATA), expected_policy_version=1)
        self.fail_next_idempotency_write()
        with self.assertRaises(sqlite3.OperationalError):
            self.service.publish_policy(ADMIN, 1, policy_payload("v2", deadline_days=45), idem_key="pub-1")
        # 旧快照原样保留：政策仍是v1，草稿期限未被改写
        self.assertEqual(self.service.current_policy(ADMIN)["version"], 1)
        unchanged = self.service.get_record(INTAKE, draft["id"])
        self.assertEqual(unchanged["payload"]["policy_version"], 1)
        self.assertEqual(unchanged["payload"]["deadline_day"], 130)
        self.assertEqual(unchanged["version"], draft["version"])
        result = self.service.publish_policy(ADMIN, 1, policy_payload("v2", deadline_days=45), idem_key="pub-1")
        self.assertEqual(result["policy"]["version"], 2)
        recalced = self.service.get_record(INTAKE, draft["id"])
        self.assertEqual(recalced["payload"]["deadline_day"], 145)
        recalc_events = [e for e in self.service.timeline(INTAKE, draft["id"]) if e["action"] == "policy_recalc"]
        self.assertEqual(len(recalc_events), 1)
        # 重放不再重复生效
        replay = self.service.publish_policy(ADMIN, 1, policy_payload("v2", deadline_days=45), idem_key="pub-1")
        self.assertEqual(replay["policy"]["version"], 2)
        self.assertEqual(self.service.current_policy(ADMIN)["version"], 2)

    def test_failed_action_keeps_old_snapshot(self):
        record = self.service.create(INTAKE, "IMM-100", dict(CREATE_DATA), expected_policy_version=1)
        self.fail_next_idempotency_write()
        with self.assertRaises(sqlite3.OperationalError):
            self.service.act(Actor("rep", "legal_rep"), record["id"], 1, "submit", {"documents": ["passport", "sponsor_letter"]}, idem_key="submit-2")
        unchanged = self.service.get_record(INTAKE, record["id"])
        self.assertEqual(unchanged["state"], "draft")
        self.assertEqual(unchanged["version"], 1)
        self.assertEqual(unchanged["payload"]["submitted_documents"], [])
        retried = self.service.act(Actor("rep", "legal_rep"), record["id"], 1, "submit", {"documents": ["passport", "sponsor_letter"]}, idem_key="submit-2")
        self.assertEqual(retried["state"], "submitted")
