"""移民案件期限与材料管理领域规则与状态转换。

政策版本化约定：
- 政策内容（各类案件的法定天数、补件期限、上诉窗口、必备材料）只在政策版本中定义。
- 受理案件时把当前政策版本与期限快照进案件payload（policy_version/policy_snapshot）。
- 草稿案件跟随最新政策：发布新版本时由服务层调用recalculate_payload重算。
- 已提交或补件中的案件钉在原版本，补件期限与上诉窗口一律取快照值。
"""
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "draft"
CASE_TYPES = ["asylum", "family", "work"]
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {'submit': {'legal_rep', 'case_officer'}, 'request_evidence': {'case_officer'}, 'respond': {'legal_rep'}, 'decide': {'case_officer', 'supervisor'}, 'appeal': {'legal_rep'}, 'close': {'supervisor'}}
TRANSITIONS = {'submit': {'draft': 'submitted'}, 'request_evidence': {'submitted': 'evidence_requested'}, 'respond': {'evidence_requested': 'response_received'}, 'decide': {'submitted': 'decided', 'response_received': 'decided'}, 'appeal': {'decided': 'appealed'}, 'close': {'decided': 'closed', 'appealed': 'closed'}}

DEFAULT_POLICY_NAME = "初始政策（系统预置）"
DEFAULT_POLICY_RULES = {
    "asylum": {"deadline_days": 180, "evidence_allowed_days": 30, "appeal_window_days": 30, "required_documents": ["passport", "personal_statement"]},
    "family": {"deadline_days": 30, "evidence_allowed_days": 10, "appeal_window_days": 30, "required_documents": ["passport", "sponsor_letter"]},
    "work": {"deadline_days": 60, "evidence_allowed_days": 14, "appeal_window_days": 30, "required_documents": ["passport", "employer_letter"]},
}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE
    CASE_TYPES = CASE_TYPES

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_publish_policy(self, role: str) -> bool:
        return role == "admin"

    def default_policy(self) -> Tuple[str, Dict[str, Any]]:
        return DEFAULT_POLICY_NAME, {case_type: dict(rule) for case_type, rule in DEFAULT_POLICY_RULES.items()}

    # ---------- 政策版本 ----------

    def validate_policy(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValidationError("政策内容必须是对象")
        name = text(payload, "name")
        rules = payload.get("rules")
        if not isinstance(rules, dict):
            raise ValidationError("rules必须是对象")
        unknown = sorted(set(rules) - set(CASE_TYPES))
        if unknown:
            raise ValidationError("rules包含未知案件类型：" + ", ".join(unknown))
        missing = [case_type for case_type in CASE_TYPES if case_type not in rules]
        if missing:
            raise ValidationError("rules缺少案件类型：" + ", ".join(missing))
        cleaned: Dict[str, Any] = {}
        for case_type in CASE_TYPES:
            section = rules[case_type]
            if not isinstance(section, dict):
                raise ValidationError("rules.%s必须是对象" % case_type)
            cleaned[case_type] = {
                "deadline_days": integer(section, "deadline_days", 1),
                "evidence_allowed_days": integer(section, "evidence_allowed_days", 1),
                "appeal_window_days": integer(section, "appeal_window_days", 0),
                "required_documents": text_list(section, "required_documents", 1),
            }
        return {"name": name, "rules": cleaned}

    # ---------- 案件 ----------

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "applicant_id")
        choice(p, "case_type", CASE_TYPES)
        integer(p, "received_day", 0)
        integer(p, "response_day", 0)
        boolean(p, "representation_active")
        return p

    def _snapshot_from_policy(self, policy: Dict[str, Any], case_type: str) -> Dict[str, Any]:
        section = policy["rules"][case_type]
        return {
            "deadline_days": int(section["deadline_days"]),
            "evidence_allowed_days": int(section["evidence_allowed_days"]),
            "appeal_window_days": int(section["appeal_window_days"]),
            "required_documents": list(section["required_documents"]),
        }

    def _apply_snapshot(self, p: Dict[str, Any]) -> Dict[str, Any]:
        snapshot = p["policy_snapshot"]
        p["deadline_days"] = int(snapshot["deadline_days"])
        p["required_documents"] = list(snapshot["required_documents"])
        p["deadline_day"] = int(p["received_day"]) + int(p["deadline_days"])
        p["days_remaining"] = int(p["deadline_day"]) - int(p["response_day"])
        p["overdue"] = p["days_remaining"] < 0
        submitted = p.get("submitted_documents") or []
        p["missing_documents"] = [doc for doc in p["required_documents"] if doc not in submitted]
        return p

    def prepare_create(self, payload: Dict[str, Any], policy: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["policy_version"] = int(policy["version"])
        p["policy_snapshot"] = self._snapshot_from_policy(policy, p["case_type"])
        p["submitted_documents"] = []
        return self._apply_snapshot(p)

    def recalculate_payload(self, payload: Dict[str, Any], policy: Dict[str, Any]) -> Dict[str, Any]:
        """草稿案件按新政策版本重算期限与材料清单，返回新payload。"""
        p = dict(payload)
        p["policy_version"] = int(policy["version"])
        p["policy_snapshot"] = self._snapshot_from_policy(policy, p["case_type"])
        return self._apply_snapshot(p)

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"closed", "decided"} and item["payload"].get("applicant_id") == payload.get("applicant_id") and item["payload"].get("case_type") == payload.get("case_type"):
                raise Conflict("同一申请人同类型案件仍在处理中")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        snapshot = p.get("policy_snapshot") or {}
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "submit":
            docs = text_list(data, "documents", 1)
            missing = [doc for doc in p["required_documents"] if doc not in docs]
            if missing and not boolean(data, "supervisor_waiver"):
                raise ValidationError("缺少材料：" + ", ".join(missing))
            if p["overdue"] and not boolean(data, "supervisor_waiver"):
                raise ValidationError("案件已超过提交期限")
            changes["submitted_documents"] = docs
            changes["missing_documents"] = missing
            changes["waiver_used"] = boolean(data, "supervisor_waiver")
            summary = "申请材料已提交"
        elif action == "request_evidence":
            request_day = integer(data, "evidence_request_day", p["response_day"])
            # 补件期限默认取案件钉住的政策快照，不随新政策变化
            default_days = int(snapshot.get("evidence_allowed_days", 1))
            allowed_days = integer(data, "allowed_days", 1) if "allowed_days" in data else default_days
            changes["evidence_request_day"] = request_day
            changes["evidence_allowed_days"] = allowed_days
            changes["evidence_due_day"] = request_day + allowed_days
            changes["evidence_request"] = text(data, "evidence_request")
            summary = "补件要求已发出"
        elif action == "respond":
            docs = text_list(data, "documents", 1)
            if int(data.get("response_day", p["response_day"])) > int(p["evidence_due_day"]):
                raise ValidationError("补件回应超过期限")
            changes["response_day"] = int(data["response_day"])
            changes["evidence_documents"] = docs
            summary = "补件已回应"
        elif action == "decide":
            changes["decision"] = choice(data, "decision", ["granted", "denied", "withdrawn"])
            changes["decision_reason"] = text(data, "decision_reason")
            summary = "案件已作出决定"
        elif action == "appeal":
            appeal_day = integer(data, "appeal_day", 0)
            window = int(snapshot.get("appeal_window_days", 30))
            if appeal_day > int(p["deadline_day"]) + window:
                raise ValidationError("上诉窗口已关闭")
            changes["appeal_day"] = appeal_day
            changes["appeal_reason"] = text(data, "appeal_reason")
            summary = "上诉已登记"
        elif action == "close":
            changes["closure_note"] = text(data, "closure_note")
            summary = "案件归档"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
