"""业务用例编排、权限检查、乐观并发、幂等与审计。

并发与失败语义：
- 每个写用例在单个事务内完成"重放检查 -> 业务校验 -> 写入 -> 记录幂等键"。
- 同一事务持写锁（BEGIN IMMEDIATE），发布与受理、两个提交动作撞车时，
  晚到的一方在版本检查处得到409，已提交的数据不会被写一半。
- 任何一步失败整个事务回滚，旧快照保留；客户端带同一幂等键重试不会重复生效。
"""
import hashlib
import json
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, integer, text
from .repository import Repository
from .rules import DomainRules


class _Replay(Exception):
    """幂等键命中：事务内短路，返回首次成功时的响应。"""

    def __init__(self, response: Dict[str, Any]) -> None:
        super().__init__("idempotent replay")
        self.response = response


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    # ---------- 身份与权限 ----------

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    @staticmethod
    def _ensure_same_org(actor: Actor, record: Dict[str, Any]) -> None:
        if record.get("organization", "") != actor.organization:
            raise PermissionDenied("无权处理其他机构的案件")

    # ---------- 幂等 ----------

    @staticmethod
    def _fingerprint(endpoint: str, payload: Dict[str, Any]) -> str:
        raw = json.dumps({"endpoint": endpoint, "payload": payload}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _check_idem_key(idem_key: Optional[str]) -> Optional[str]:
        if idem_key is None:
            return None
        key = idem_key.strip()
        if not key:
            return None
        if len(key) > 200:
            raise ValidationError("Idempotency-Key过长")
        return key

    def _replay_guard(self, conn, actor: Actor, idem_key: Optional[str], endpoint: str, fingerprint: str) -> None:
        """在写事务内调用：命中幂等键则重放旧响应，键被不同请求占用则409。"""
        if not idem_key:
            return
        existing = self.repository.find_idempotency(conn, actor.user_id, idem_key)
        if existing is None:
            return
        if existing["fingerprint"] != fingerprint or existing["endpoint"] != endpoint:
            raise Conflict("幂等键已被其他请求使用")
        raise _Replay(existing["response"])

    def _store_idem(self, conn, actor: Actor, idem_key: Optional[str], endpoint: str, fingerprint: str, response: Dict[str, Any]) -> None:
        if idem_key:
            self.repository.store_idempotency(conn, actor.user_id, idem_key, endpoint, fingerprint, response)

    # ---------- 案件用例 ----------

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any], expected_policy_version: int, idem_key: Optional[str] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        expected_policy_version = integer({"expected_policy_version": expected_policy_version}, "expected_policy_version", 1)
        payload = dict(payload or {})
        idem_key = self._check_idem_key(idem_key)
        endpoint = "create"
        fingerprint = self._fingerprint(endpoint, {"reference": reference, "data": payload, "expected_policy_version": expected_policy_version})
        try:
            with self.repository.transaction() as conn:
                self._replay_guard(conn, actor, idem_key, endpoint, fingerprint)
                policy = self.repository.current_policy(conn)
                if int(policy["version"]) != expected_policy_version:
                    raise Conflict("政策已发布新版本（当前v%s），请核对后重新受理" % policy["version"])
                prepared = self.rules.prepare_create(payload, policy)
                self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500, conn=conn))
                record = self.repository.insert_record(conn, reference, self.rules.INITIAL_STATE, prepared, actor.user_id, actor.organization)
                self._store_idem(conn, actor, idem_key, endpoint, fingerprint, record)
                return record
        except _Replay as replay:
            return replay.response

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit, organization=actor.organization)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        self._ensure_same_org(actor, record)
        return record

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any], idem_key: Optional[str] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        expected_version = integer({"expected_version": expected_version}, "expected_version", 1)
        data = dict(data or {})
        idem_key = self._check_idem_key(idem_key)
        endpoint = "act:%s" % action
        fingerprint = self._fingerprint(endpoint, {"record_id": int(record_id), "expected_version": expected_version, "data": data})
        try:
            with self.repository.transaction() as conn:
                self._replay_guard(conn, actor, idem_key, endpoint, fingerprint)
                record = self.repository.get(record_id, conn)
                self._ensure_same_org(actor, record)
                if int(record["version"]) != expected_version:
                    raise Conflict("版本冲突，请刷新后重试")
                new_state, new_payload, summary = self.rules.apply_action(record, action, data)
                updated = self.repository.apply_mutation(
                    conn,
                    record_id=int(record_id),
                    expected_version=expected_version,
                    state=new_state,
                    payload=new_payload,
                    actor_id=actor.user_id,
                    action=action,
                    details={"summary": summary, "input": data, "from": record["state"], "to": new_state, "policy_version": new_payload.get("policy_version")},
                )
                self._store_idem(conn, actor, idem_key, endpoint, fingerprint, updated)
                return updated
        except _Replay as replay:
            return replay.response

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        self._ensure_same_org(actor, record)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats(organization=actor.organization)

    # ---------- 政策版本用例 ----------

    def list_policies(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_policies()

    def current_policy(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.current_policy()

    def publish_policy(self, actor: Actor, expected_version: int, payload: Dict[str, Any], idem_key: Optional[str] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_publish_policy(actor.role):
            raise PermissionDenied("只有管理员可以发布政策版本")
        expected_version = integer({"expected_version": expected_version}, "expected_version", 1)
        cleaned = self.rules.validate_policy(payload or {})
        idem_key = self._check_idem_key(idem_key)
        endpoint = "publish_policy"
        fingerprint = self._fingerprint(endpoint, {"expected_version": expected_version, "policy": cleaned})
        try:
            with self.repository.transaction() as conn:
                self._replay_guard(conn, actor, idem_key, endpoint, fingerprint)
                current = self.repository.current_policy(conn)
                if int(current["version"]) != expected_version:
                    raise Conflict("政策版本冲突：当前版本为v%s，请刷新后重试" % current["version"])
                policy = self.repository.insert_policy(conn, cleaned["name"], cleaned["rules"], actor.user_id)
                recalculated = self._recalculate_drafts(conn, policy, actor)
                result = {"policy": policy, "recalculated_drafts": recalculated}
                self._store_idem(conn, actor, idem_key, endpoint, fingerprint, result)
                return result
        except _Replay as replay:
            return replay.response

    def rollback_policy(self, actor: Actor, expected_version: int, to_version: int, name: Optional[str] = None, idem_key: Optional[str] = None) -> Dict[str, Any]:
        """回滚只生成新版本：复制目标旧版本内容，追加为最新版本，历史不改动。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_publish_policy(actor.role):
            raise PermissionDenied("只有管理员可以回滚政策")
        expected_version = integer({"expected_version": expected_version}, "expected_version", 1)
        to_version = integer({"to_version": to_version}, "to_version", 1)
        idem_key = self._check_idem_key(idem_key)
        endpoint = "rollback_policy"
        fingerprint = self._fingerprint(endpoint, {"expected_version": expected_version, "to_version": to_version, "name": name or ""})
        try:
            with self.repository.transaction() as conn:
                self._replay_guard(conn, actor, idem_key, endpoint, fingerprint)
                current = self.repository.current_policy(conn)
                if int(current["version"]) != expected_version:
                    raise Conflict("政策版本冲突：当前版本为v%s，请刷新后重试" % current["version"])
                if to_version >= int(current["version"]):
                    raise ValidationError("回滚目标必须是早于当前版本的历史版本")
                source = self.repository.get_policy_by_version(to_version, conn)
                new_name = (name or "").strip() or ("%s（回滚自v%s）" % (source["name"], to_version))
                policy = self.repository.insert_policy(conn, new_name, source["rules"], actor.user_id, source_version=to_version)
                recalculated = self._recalculate_drafts(conn, policy, actor)
                result = {"policy": policy, "recalculated_drafts": recalculated}
                self._store_idem(conn, actor, idem_key, endpoint, fingerprint, result)
                return result
        except _Replay as replay:
            return replay.response

    def _recalculate_drafts(self, conn, policy: Dict[str, Any], actor: Actor) -> int:
        """发布/回滚事务内重算全部草稿案件；已提交及之后的案件钉在原版本不动。"""
        drafts = self.repository.list_records(state=self.rules.INITIAL_STATE, limit=500, conn=conn)
        count = 0
        for draft in drafts:
            old = draft["payload"]
            new_payload = self.rules.recalculate_payload(old, policy)
            self.repository.apply_mutation(
                conn,
                record_id=draft["id"],
                expected_version=draft["version"],
                state=draft["state"],
                payload=new_payload,
                actor_id=actor.user_id,
                action="policy_recalc",
                details={
                    "summary": "政策v%s发布，草稿按新版本重算" % policy["version"],
                    "from_policy": old.get("policy_version"),
                    "to_policy": policy["version"],
                    "old_deadline_day": old.get("deadline_day"),
                    "new_deadline_day": new_payload.get("deadline_day"),
                },
            )
            count += 1
        return count
