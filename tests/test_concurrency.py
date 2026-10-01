import copy
import tempfile
import threading
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


def run_parallel(*tasks):
    """所有任务在同一道栅栏后起跑，收集(结果, 异常)。"""
    barrier = threading.Barrier(len(tasks))
    outcomes = []

    def wrap(fn):
        def runner():
            barrier.wait(timeout=10)
            try:
                outcomes.append((fn(), None))
            except Exception as exc:
                outcomes.append((None, exc))
        return runner

    threads = [threading.Thread(target=wrap(task)) for task in tasks]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return outcomes


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def create_case(self, reference="IMM-100"):
        return self.service.create(INTAKE, reference, dict(CREATE_DATA), expected_policy_version=1)

    def test_two_clerks_submit_exactly_one_wins(self):
        record = self.create_case()
        docs = {"documents": ["passport", "sponsor_letter"]}
        outcomes = run_parallel(
            lambda: self.service.act(Actor("clerk-a", "legal_rep"), record["id"], 1, "submit", docs),
            lambda: self.service.act(Actor("clerk-b", "case_officer"), record["id"], 1, "submit", docs),
        )
        successes = [result for result, exc in outcomes if exc is None]
        conflicts = [exc for result, exc in outcomes if isinstance(exc, Conflict)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 1)
        final = self.service.get_record(INTAKE, record["id"])
        self.assertEqual(final["state"], "submitted")
        self.assertEqual(final["version"], 2)
        submits = [event for event in self.service.timeline(INTAKE, record["id"]) if event["action"] == "submit"]
        self.assertEqual(len(submits), 1)

    def test_publish_and_intake_race_has_no_half_writes(self):
        def publish():
            return self.service.publish_policy(ADMIN, 1, policy_payload("v2", deadline_days=45))

        def intake():
            return self.service.create(INTAKE, "IMM-200", dict(CREATE_DATA), expected_policy_version=1)

        outcomes = run_parallel(publish, intake)
        current = self.service.current_policy(ADMIN)
        self.assertEqual(current["version"], 2)
        records = self.service.list_records(INTAKE)
        intake_failed = any(isinstance(exc, Conflict) for result, exc in outcomes)
        if intake_failed:
            # 发布先到：受理必须整体失败，不能留下按旧政策写的半个案件
            self.assertEqual(records, [])
        else:
            # 受理先到：案件按v1快照合法成立，随后发布把它当草稿重算到v2
            self.assertEqual(len(records), 1)
            payload = records[0]["payload"]
            self.assertEqual(payload["policy_version"], 2)
            self.assertEqual(payload["deadline_day"], 145)
            self.assertEqual(payload["policy_snapshot"]["deadline_days"], 45)

    def test_two_publishes_exactly_one_wins(self):
        outcomes = run_parallel(
            lambda: self.service.publish_policy(ADMIN, 1, policy_payload("v2-a", deadline_days=45)),
            lambda: self.service.publish_policy(ADMIN, 1, policy_payload("v2-b", deadline_days=50)),
        )
        successes = [result for result, exc in outcomes if exc is None]
        conflicts = [exc for result, exc in outcomes if isinstance(exc, Conflict)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 1)
        policies = self.service.list_policies(ADMIN)
        self.assertEqual([p["version"] for p in policies], [2, 1])

    def test_publish_recalc_bumps_draft_version_so_stale_submit_conflicts(self):
        record = self.create_case()
        self.service.publish_policy(ADMIN, 1, policy_payload("v2", deadline_days=45))
        with self.assertRaises(Conflict):
            self.service.act(Actor("rep", "legal_rep"), record["id"], record["version"], "submit", {"documents": ["passport", "sponsor_letter"]})
