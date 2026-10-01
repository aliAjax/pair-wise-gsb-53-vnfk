"""政策版本化行为测试：快照冻结、发布重算、回滚只追加、撞车冲突、失败原子性、机构隔离。"""
import copy
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied
from src.rules import SEED_POLICY


def intake_officer(org="org-a", user="creator"):
    return Actor(user, "intake_officer", org)


def admin():
    return Actor("root", "admin", "hq")


def legal_rep(org="org-a", user="rep"):
    return Actor(user, "legal_rep", org)


def case_officer(org="org-a"):
    return Actor("officer", "case_officer", org)


def create_payload(applicant="A-900", received_day=100, response_day=110):
    return {
        "applicant_id": applicant,
        "case_type": "family",
        "received_day": received_day,
        "response_day": response_day,
        "representation_active": True,
    }


def new_family_policy(deadline_days=45, evidence_days=20, docs=None):
    content = copy.deepcopy(SEED_POLICY)
    content["deadline_days"]["family"] = deadline_days
    content["evidence_days"]["family"] = evidence_days
    if docs is not None:
        content["required_documents"]["family"] = docs
    return content


class PolicyVersionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_seed_policy_exists(self):
        current = self.service.current_policy(intake_officer())
        self.assertEqual(current["version"], 1)
        self.assertEqual(current["content"], SEED_POLICY)
        self.assertEqual(len(self.service.list_policies(admin())), 1)

    def test_publish_rebases_drafts_only(self):
        # 草稿与已提交案件各一（不同申请人，避免同申请人在办冲突）。
        draft = self.service.create(intake_officer(), "IMM-D1", create_payload("A-100"))
        submitted = self.service.create(intake_officer(), "IMM-S1", create_payload("A-200"))
        submitted = self.service.act(
            legal_rep(), submitted["id"], submitted["version"], "submit",
            {"documents": ["passport", "sponsor_letter"]},
        )
        self.assertEqual(submitted["state"], "submitted")
        self.assertEqual(submitted["policy_version"], 1)

        v2 = self.service.publish_policy(admin(), 1, new_family_policy(deadline_days=45, evidence_days=20))
        self.assertEqual(v2["version"], 2)
        self.assertFalse(v2["rollback"])

        # 草稿按新版本重算：期限与材料清单切换。
        draft2 = self.service.get_record(intake_officer(), draft["id"])
        self.assertEqual(draft2["policy_version"], 2)
        self.assertEqual(draft2["payload"]["policy"]["version"], 2)
        self.assertEqual(draft2["payload"]["deadline_day"], 145)
        # 已提交案件继续认原版本。
        submitted2 = self.service.get_record(intake_officer(), submitted["id"])
        self.assertEqual(submitted2["policy_version"], 1)
        self.assertEqual(submitted2["payload"]["deadline_day"], 130)

        # 补件期限随原版本走（v1 family = 10天），不受v2的20天影响。
        submitted2 = self.service.act(
            case_officer(), submitted2["id"], submitted2["version"], "request_evidence",
            {"evidence_request_day": 115, "evidence_request": "补收入证明"},
        )
        self.assertEqual(submitted2["payload"]["allowed_days"], 10)
        self.assertEqual(submitted2["payload"]["evidence_due_day"], 125)
        self.assertEqual(submitted2["payload"]["evidence_policy_version"], 1)

        timeline = self.service.timeline(intake_officer(), draft["id"])
        self.assertTrue(any(event["action"] == "policy_rebased" for event in timeline))

    def test_publish_uses_optimistic_version(self):
        self.service.publish_policy(admin(), 1, new_family_policy(45))
        with self.assertRaises(Conflict):
            self.service.publish_policy(admin(), 1, new_family_policy(50))

    def test_non_admin_cannot_publish(self):
        with self.assertRaises(PermissionDenied):
            self.service.publish_policy(case_officer(), 1, new_family_policy())

    def test_rollback_creates_new_version_never_mutates_history(self):
        v2 = self.service.publish_policy(admin(), 1, new_family_policy(45))
        draft = self.service.create(intake_officer(), "IMM-D9", create_payload())
        self.assertEqual(draft["policy_version"], 2)

        # 回滚到v1：只生成v3，内容与v1相同。
        v3 = self.service.rollback_policy(admin(), 1)
        self.assertEqual(v3["version"], 3)
        self.assertTrue(v3["rollback"])
        self.assertEqual(v3["source_version"], 1)
        self.assertEqual(v3["content"], SEED_POLICY)

        policies = self.service.list_policies(admin())
        self.assertEqual([p["version"] for p in policies], [1, 2, 3])
        v2_fresh = self.service.get_policy(admin(), 2)
        self.assertEqual(v2_fresh["content"]["deadline_days"]["family"], 45)  # 历史行不变

        rebased = self.service.get_record(intake_officer(), draft["id"])
        self.assertEqual(rebased["policy_version"], 3)
        self.assertEqual(rebased["payload"]["deadline_day"], 130)

    def test_two_clerks_submitting_simultaneously_one_conflicts(self):
        record = self.service.create(intake_officer(), "IMM-C1", create_payload())
        results = []

        def submit(user):
            try:
                updated = self.service.act(
                    legal_rep(user=user), record["id"], record["version"], "submit",
                    {"documents": ["passport", "sponsor_letter"]},
                )
                results.append(("ok", updated["version"]))
            except Conflict as exc:
                results.append(("conflict", str(exc)))

        t1 = threading.Thread(target=submit, args=("rep-1",))
        t2 = threading.Thread(target=submit, args=("rep-2",))
        t1.start(); t2.start(); t1.join(); t2.join()

        statuses = sorted(r[0] for r in results)
        self.assertEqual(statuses, ["conflict", "ok"])
        final = self.service.get_record(intake_officer(), record["id"])
        self.assertEqual(final["state"], "submitted")
        self.assertEqual(final["version"], 2)
        timeline = self.service.timeline(intake_officer(), record["id"])
        self.assertEqual(len([e for e in timeline if e["action"] == "submit"]), 1)

    def test_intake_racing_with_publish_loser_sees_conflict(self):
        # 确定性复现“受理锁前读到v1、准备材料期间管理员发布v2”的撞车窗口：
        # 在受理开写锁之前同步完成发布，锁内版本CAS失败，受理整体回滚。
        def hook():
            self.service.publish_policy(admin(), 1, new_family_policy(45))

        self.service.policy_load_hook = hook
        with self.assertRaises(Conflict) as ctx:
            self.service.create(intake_officer(), "IMM-RACE", create_payload())
        self.assertIn("相撞", str(ctx.exception))
        self.service.policy_load_hook = None

        # 失败后没有留下半成品案件；按新版本重新受理，期限按v2计算，重试不产生重复。
        self.assertEqual(self.service.list_records(intake_officer()), [])
        retry = self.service.create(
            intake_officer(), "IMM-RACE", create_payload(), expected_policy_version=2
        )
        self.assertEqual(retry["policy_version"], 2)
        self.assertEqual(retry["payload"]["deadline_day"], 145)

    def test_failed_write_keeps_old_snapshot_and_retry_does_not_duplicate(self):
        record = self.service.create(intake_officer(), "IMM-F1", create_payload())
        old_payload = copy.deepcopy(record["payload"])
        before_timeline = len(self.service.timeline(intake_officer(), record["id"]))

        real_connect = self.service.repository._connect

        class FailingConnection:
            """代理真实连接，只拦截commit，其余方法原样委托。"""

            def __init__(self, real, fail):
                object.__setattr__(self, "_real", real)
                object.__setattr__(self, "_fail", fail)

            def commit(self):
                if self._fail["on"]:
                    raise sqlite3.OperationalError("simulated write failure")
                return self._real.commit()

            def __enter__(self):
                self._real.__enter__()
                return self

            def __exit__(self, exc_type, exc, tb):
                return self._real.__exit__(exc_type, exc, tb)

            def __getattr__(self, name):
                return getattr(object.__getattribute__(self, "_real"), name)

            def __setattr__(self, name, value):
                setattr(self._real, name, value)

        fail = {"on": True}

        def flaky_connect():
            return FailingConnection(real_connect(), fail)

        self.service.repository._connect = flaky_connect
        with self.assertRaises(sqlite3.OperationalError):
            self.service.act(
                legal_rep(), record["id"], record["version"], "submit",
                {"documents": ["passport", "sponsor_letter"]},
            )
        self.service.repository._connect = real_connect
        fail["on"] = False

        # 旧快照与版本原样保留，没有半写入的材料。
        kept = self.service.get_record(intake_officer(), record["id"])
        self.assertEqual(kept["version"], 1)
        self.assertEqual(kept["state"], "draft")
        self.assertEqual(kept["payload"], old_payload)
        self.assertEqual(
            len(self.service.timeline(intake_officer(), record["id"])), before_timeline
        )
        # 重试成功且只生效一次。
        retried = self.service.act(
            legal_rep(), record["id"], record["version"], "submit",
            {"documents": ["passport", "sponsor_letter"]},
        )
        self.assertEqual(retried["state"], "submitted")
        self.assertEqual(retried["version"], 2)
        self.assertEqual(
            len([e for e in self.service.timeline(intake_officer(), record["id"]) if e["action"] == "submit"]),
            1,
        )

    def test_cross_org_access_denied(self):
        self.service.create(intake_officer(org="org-a"), "IMM-O1", create_payload())
        outsider = intake_officer(org="org-b", user="other-clerk")
        with self.assertRaises(PermissionDenied):
            self.service.get_record(outsider, 1)
        with self.assertRaises(PermissionDenied):
            self.service.act(
                legal_rep(org="org-b"), 1, 1, "submit",
                {"documents": ["passport", "sponsor_letter"]},
            )
        with self.assertRaises(PermissionDenied):
            self.service.timeline(outsider, 1)
        # 列表也只看到本机构（草稿被机构隔离）。
        self.assertEqual(self.service.list_records(outsider), [])

    def test_intake_idempotent_retry_on_unique_reference(self):
        record = self.service.create(intake_officer(), "IMM-DUP", create_payload())
        with self.assertRaises(Conflict):
            self.service.create(intake_officer(), "IMM-DUP", create_payload())
        self.assertEqual(len(self.service.list_records(intake_officer())), 1)
        self.assertEqual(record["policy_version"], 1)


if __name__ == "__main__":
    unittest.main()
