"""业务用例编排：政策版本、机构隔离、乐观并发与审计。"""
from typing import Any, Callable, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        # 测试钩子：受理事务读到政策指针后、取政策内容前调用，用于制造“发布先到”的撞车窗口。
        self.policy_load_hook: Optional[Callable[[], None]] = None

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    @staticmethod
    def _same_org(actor: Actor, record: Dict[str, Any]) -> None:
        # 越权处理别的机构案件一律拒绝；未归属机构的案件只接受未携带机构的身份。
        if actor.organization != record.get("org", ""):
            raise PermissionDenied("无权操作其他机构的案件")

    # ---- 政策版本 ----
    def current_policy(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.current_policy()

    def list_policies(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_policies()

    def get_policy(self, actor: Actor, version: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_policy(int(version))

    def publish_policy(self, actor: Actor, expected_version: int, content: Any) -> Dict[str, Any]:
        actor = self._actor(actor)
        if not self.rules.role_can_publish(actor.role):
            raise PermissionDenied("仅管理员可发布政策版本")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            from .domain import ValidationError
            raise ValidationError("expected_version必须是整数")
        clean_content = self.rules.validate_policy_content(content)
        return self.repository.publish_policy(
            expected_version=expected_version,
            content=clean_content,
            actor_id=actor.user_id,
        )

    def rollback_policy(self, actor: Actor, target_version: int, expected_version: int = None) -> Dict[str, Any]:
        """政策回滚：把历史版本的内容复制成一个全新版本发布（旧行不动）。"""
        actor = self._actor(actor)
        if not self.rules.role_can_publish(actor.role):
            raise PermissionDenied("仅管理员可回滚政策版本")
        target = self.repository.get_policy(int(target_version))
        current = self.repository.current_policy()
        base = int(current["version"]) if expected_version is None else int(expected_version)
        return self.repository.publish_policy(
            expected_version=base,
            content=target["content"],
            actor_id=actor.user_id,
            source_version=int(target["version"]),
            rollback=True,
        )

    # ---- 案件 ----
    def create(self, actor: Actor, reference: str, payload: Dict[str, Any], expected_policy_version: int = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权受理案件")
        reference = text({"reference": reference}, "reference")
        if expected_policy_version is not None and (
            not isinstance(expected_policy_version, int) or isinstance(expected_policy_version, bool)
        ):
            from .domain import ValidationError
            raise ValidationError("expected_policy_version必须是整数")
        # 先按当前政策准备快照；仓储在写锁内会再按锁内政策重算一次并做版本CAS。
        policy = self.repository.current_policy()
        prepared = self.rules.prepare_create(payload or {}, policy)
        self.rules.check_create_conflicts(
            prepared, self.repository.list_records(org=actor.organization, limit=500)
        )
        base_version = (
            int(expected_policy_version) if expected_policy_version is not None else int(policy["version"])
        )
        return self.repository.create(
            reference=reference,
            org=actor.organization,
            payload=prepared,
            actor_id=actor.user_id,
            expected_policy_version=base_version,
            policy_load_hook=self.policy_load_hook,
        )

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(org=actor.organization, state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        self._same_org(actor, record)
        return record

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self._same_org(actor, record)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={
                "summary": summary,
                "input": data or {},
                "policy_version": new_payload["policy"]["version"],
                "from": record["state"],
                "to": new_state,
            },
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        self._same_org(actor, record)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
