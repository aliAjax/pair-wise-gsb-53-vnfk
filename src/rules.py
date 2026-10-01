"""移民案件政策版本、期限快照、材料完整性与状态转换规则。

政策（法定期限、补件天数、必备材料）会频繁调整，但案件只认受理那一刻的政策：
受理时把政策内容整体快照进案件；只有草稿案件会在新版本发布时重算，
一旦提交（含补件中），期限与补件窗口永远跟随原快照。
"""
import copy
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Conflict, ValidationError, boolean, choice, integer, text, text_list


CASE_TYPES: Tuple[str, ...] = ("asylum", "family", "work")

INITIAL_STATE = "draft"
CREATE_ROLES = {"intake_officer"}
PUBLISH_ROLES = {"admin"}
ACTION_ROLES = {
    "submit": {"legal_rep", "case_officer"},
    "request_evidence": {"case_officer"},
    "respond": {"legal_rep"},
    "decide": {"case_officer", "supervisor"},
    "appeal": {"legal_rep"},
    "close": {"supervisor"},
}
TRANSITIONS = {
    "submit": {"draft": "submitted"},
    "request_evidence": {"submitted": "evidence_requested"},
    "respond": {"evidence_requested": "response_received"},
    "decide": {"submitted": "decided", "response_received": "decided"},
    "appeal": {"decided": "appealed"},
    "close": {"decided": "closed", "appealed": "closed"},
}

# 初始政策（版本1），服务首次初始化时落库，之后只能追加新版本，不能改写。
SEED_POLICY: Dict[str, Any] = {
    "deadline_days": {"asylum": 60, "family": 30, "work": 20},
    "evidence_days": {"asylum": 30, "family": 10, "work": 10},
    "required_documents": {
        "asylum": ["passport", "asylum_statement"],
        "family": ["passport", "sponsor_letter"],
        "work": ["passport", "job_offer"],
    },
    "appeal_days": 30,
}

# 发布新版本后仍停留在这些状态的案件，一律保留受理时的政策快照。
REBASE_STATES = {INITIAL_STATE}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    # ---- 角色 ----
    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES) | PUBLISH_ROLES
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role in ACTION_ROLES.get(action, set())

    def role_can_publish(self, role: str) -> bool:
        return role in PUBLISH_ROLES

    # ---- 政策内容 ----
    @staticmethod
    def seed_content() -> Dict[str, Any]:
        return copy.deepcopy(SEED_POLICY)

    def validate_policy_content(self, raw: Any) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValidationError("政策内容必须是对象")
        content = {
            "deadline_days": self._days_map(raw, "deadline_days"),
            "evidence_days": self._days_map(raw, "evidence_days"),
            "required_documents": self._documents_map(raw),
            "appeal_days": integer(raw, "appeal_days", 1),
        }
        return content

    @staticmethod
    def _days_map(data: Dict[str, Any], key: str) -> Dict[str, int]:
        value = data.get(key)
        if not isinstance(value, dict):
            raise ValidationError("%s必须是案件类型到天数的映射" % key)
        if set(value.keys()) != set(CASE_TYPES):
            raise ValidationError("%s必须且只能包含%s" % (key, "/".join(CASE_TYPES)))
        result: Dict[str, int] = {}
        for case_type in CASE_TYPES:
            days = value[case_type]
            if isinstance(days, bool) or not isinstance(days, int) or days < 1:
                raise ValidationError("%s.%s必须是正整数" % (key, case_type))
            result[case_type] = days
        return result

    @staticmethod
    def _documents_map(data: Dict[str, Any]) -> Dict[str, List[str]]:
        value = data.get("required_documents")
        if not isinstance(value, dict):
            raise ValidationError("required_documents必须是案件类型到材料列表的映射")
        if set(value.keys()) != set(CASE_TYPES):
            raise ValidationError("required_documents必须且只能包含%s" % "/".join(CASE_TYPES))
        result: Dict[str, List[str]] = {}
        for case_type in CASE_TYPES:
            docs = value[case_type]
            if not isinstance(docs, list) or not docs or any(
                not isinstance(doc, str) or not doc.strip() for doc in docs
            ):
                raise ValidationError("required_documents.%s必须是非空文本列表" % case_type)
            result[case_type] = [doc.strip() for doc in docs]
        return result

    # ---- 受理：把当时的政策和期限留给案件 ----
    def prepare_create(self, payload: Dict[str, Any], policy: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        text(p, "applicant_id")
        case_type = choice(p, "case_type", list(CASE_TYPES))
        received_day = integer(p, "received_day", 0)
        response_day = integer(p, "response_day", 0)
        boolean(p, "representation_active")
        content = self._policy_content(policy)
        version = int(policy["version"])

        deadline_days = int(content["deadline_days"][case_type])
        required_documents = list(content["required_documents"][case_type])
        deadline_day = received_day + deadline_days
        return {
            "applicant_id": p["applicant_id"],
            "case_type": case_type,
            "received_day": received_day,
            "response_day": response_day,
            "representation_active": boolean(p, "representation_active"),
            "policy": {"version": version, "content": copy.deepcopy(content)},
            "deadline_days": deadline_days,
            "deadline_day": deadline_day,
            "days_remaining": deadline_day - response_day,
            "overdue": deadline_day - response_day < 0,
            "required_documents": required_documents,
            "submitted_documents": [],
            "missing_documents": list(required_documents),
        }

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if (
                item["state"] not in {"closed", "decided"}
                and item["payload"].get("applicant_id") == payload.get("applicant_id")
                and item["payload"].get("case_type") == payload.get("case_type")
            ):
                raise Conflict("同一申请人同类型案件仍在处理中")

    # ---- 草稿案件按新版本重算 ----
    def rebase_draft(self, payload: Dict[str, Any], new_version: int, content: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        case_type = p["case_type"]
        deadline_days = int(content["deadline_days"][case_type])
        deadline_day = int(p["received_day"]) + deadline_days
        required_documents = list(content["required_documents"][case_type])
        submitted = p.get("submitted_documents", [])
        p["policy"] = {"version": int(new_version), "content": copy.deepcopy(content)}
        p["deadline_days"] = deadline_days
        p["deadline_day"] = deadline_day
        p["days_remaining"] = deadline_day - int(p.get("response_day", 0))
        p["overdue"] = p["days_remaining"] < 0
        p["required_documents"] = required_documents
        p["missing_documents"] = [doc for doc in required_documents if doc not in submitted]
        return p

    def needs_rebase(self, state: str) -> bool:
        return state in REBASE_STATES

    # ---- 状态转换 ----
    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
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
            summary = "申请材料已提交（按政策v%s）" % self._snapshot_version(p)
        elif action == "request_evidence":
            # 补件期限随案件受理时的原版本政策走，不接受请求方自行指定天数。
            content = self._snapshot_content(p)
            request_day = integer(data, "evidence_request_day", int(p.get("response_day", 0)))
            allowed_days = int(content["evidence_days"][p["case_type"]])
            changes["evidence_request_day"] = request_day
            changes["allowed_days"] = allowed_days
            changes["evidence_due_day"] = request_day + allowed_days
            changes["evidence_policy_version"] = self._snapshot_version(p)
            changes["evidence_request"] = text(data, "evidence_request")
            summary = "补件要求已发出（补件期限按政策v%s：%s天）" % (
                changes["evidence_policy_version"],
                allowed_days,
            )
        elif action == "respond":
            response_day = integer(data, "response_day", 0)
            if response_day > int(p["evidence_due_day"]):
                raise ValidationError("补件回应超过期限")
            docs = text_list(data, "documents", 1)
            changes["response_day"] = response_day
            changes["evidence_documents"] = docs
            summary = "补件已回应"
        elif action == "decide":
            changes["decision"] = choice(data, "decision", ["granted", "denied", "withdrawn"])
            changes["decision_reason"] = text(data, "decision_reason")
            summary = "案件已作出决定"
        elif action == "appeal":
            appeal_day = integer(data, "appeal_day", 0)
            appeal_window = int(self._snapshot_content(p).get("appeal_days", 30))
            if appeal_day > int(p["deadline_day"]) + appeal_window:
                raise ValidationError("上诉窗口已关闭")
            changes["appeal_day"] = appeal_day
            changes["appeal_reason"] = text(data, "appeal_reason")
            summary = "上诉已登记"
        elif action == "close":
            changes["closure_note"] = text(data, "closure_note")
            summary = "案件归档"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    # ---- 快照辅助 ----
    @staticmethod
    def _policy_content(policy: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(policy, dict) or "version" not in policy or "content" not in policy:
            raise ValidationError("政策版本缺失")
        return policy["content"]

    @staticmethod
    def _snapshot_content(payload: Dict[str, Any]) -> Dict[str, Any]:
        snapshot = payload.get("policy")
        if not isinstance(snapshot, dict) or "content" not in snapshot:
            raise ValidationError("案件缺少政策快照")
        return snapshot["content"]

    @staticmethod
    def _snapshot_version(payload: Dict[str, Any]) -> int:
        snapshot = payload.get("policy")
        if not isinstance(snapshot, dict) or "version" not in snapshot:
            raise ValidationError("案件缺少政策快照")
        return int(snapshot["version"])
