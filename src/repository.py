"""SQLite 表结构与事务访问。

所有多步写入都在单个 BEGIN IMMEDIATE 事务里完成：
- 案件创建/动作：记录更新 + 审计事件 + 幂等键一起提交或一起回滚。
- 政策发布/回滚：新版本插入 + 全部草稿重算 + 幂等键一起提交或一起回滚。
写入失败时旧快照原样保留，不会出现政策快照和材料各写一半的情况。
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    @contextmanager
    def transaction(self):
        """单连接写事务：进入即取写锁，提交或整体回滚。"""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    organization TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS policies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version INTEGER NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    rules TEXT NOT NULL,
                    source_version INTEGER,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    response TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (actor_id, idem_key)
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_records_org ON records(organization);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(records)").fetchall()}
            if "organization" not in columns:
                connection.execute("ALTER TABLE records ADD COLUMN organization TEXT NOT NULL DEFAULT ''")

    # ---------- 基础工具 ----------

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _policy_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["rules"] = json.loads(item["rules"])
        return item

    def _query_one(self, conn: Optional[sqlite3.Connection], sql: str, params: tuple) -> Optional[sqlite3.Row]:
        if conn is not None:
            return conn.execute(sql, params).fetchone()
        with self._connect() as connection:
            return connection.execute(sql, params).fetchone()

    def _query_all(self, conn: Optional[sqlite3.Connection], sql: str, params: tuple) -> List[sqlite3.Row]:
        if conn is not None:
            return conn.execute(sql, params).fetchall()
        with self._connect() as connection:
            return connection.execute(sql, params).fetchall()

    # ---------- 政策版本 ----------

    def ensure_seed_policy(self, name: str, rules: Dict[str, Any]) -> None:
        """首次启动时写入政策v1；已存在任何版本则跳过（幂等）。"""
        with self.transaction() as conn:
            row = conn.execute("SELECT COUNT(*) AS total FROM policies").fetchone()
            if int(row["total"]) == 0:
                conn.execute(
                    "INSERT INTO policies(version,name,rules,source_version,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (1, name, json.dumps(rules, ensure_ascii=False, sort_keys=True), None, "system", _now()),
                )

    def current_policy(self, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
        row = self._query_one(conn, "SELECT * FROM policies ORDER BY version DESC LIMIT 1", ())
        if row is None:
            raise NotFound("尚未发布任何政策版本")
        return self._policy_row(row)

    def get_policy_by_version(self, version: int, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
        row = self._query_one(conn, "SELECT * FROM policies WHERE version=?", (int(version),))
        if row is None:
            raise NotFound("政策版本%s不存在" % version)
        return self._policy_row(row)

    def list_policies(self) -> List[Dict[str, Any]]:
        rows = self._query_all(None, "SELECT * FROM policies ORDER BY version DESC", ())
        return [self._policy_row(row) for row in rows]

    def insert_policy(self, conn: sqlite3.Connection, name: str, rules: Dict[str, Any], actor_id: str, source_version: Optional[int] = None) -> Dict[str, Any]:
        """在调用方事务内追加新版本。版本号=当前最大+1，历史版本永不修改。"""
        current = self.current_policy(conn)
        version = int(current["version"]) + 1
        conn.execute(
            "INSERT INTO policies(version,name,rules,source_version,created_by,created_at) VALUES(?,?,?,?,?,?)",
            (version, name, json.dumps(rules, ensure_ascii=False, sort_keys=True), source_version, actor_id, _now()),
        )
        return self.get_policy_by_version(version, conn)

    # ---------- 案件记录 ----------

    def insert_record(self, conn: sqlite3.Connection, reference: str, state: str, payload: Dict[str, Any], actor_id: str, organization: str) -> Dict[str, Any]:
        now = _now()
        try:
            cursor = conn.execute(
                "INSERT INTO records(reference,state,version,organization,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (reference, state, 1, organization, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        record_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, "created", actor_id, 1, json.dumps({"state": state, "policy_version": payload.get("policy_version")}, ensure_ascii=False, sort_keys=True), now),
        )
        row = conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return self._row(row)

    def get(self, record_id: int, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
        row = self._query_one(conn, "SELECT * FROM records WHERE id=?", (int(record_id),))
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100, organization: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses, params = [], []
        if state:
            clauses.append("state=?")
            params.append(state)
        if organization is not None:
            clauses.append("organization=?")
            params.append(organization)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._query_all(conn, "SELECT * FROM records%s ORDER BY id DESC LIMIT ?" % where, tuple(params) + (limit,))
        return [self._row(row) for row in rows]

    def apply_mutation(self, conn: sqlite3.Connection, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        """在调用方事务内做乐观锁更新：版本不匹配即冲突，记录与审计同生共死。"""
        now = _now()
        row = conn.execute("SELECT version FROM records WHERE id=?", (int(record_id),)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        if int(row["version"]) != int(expected_version):
            raise Conflict("版本冲突，请刷新后重试")
        version = int(expected_version) + 1
        conn.execute(
            "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
            (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, int(record_id)),
        )
        conn.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (int(record_id), action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
        )
        return self.get(record_id, conn)

    # ---------- 幂等键 ----------

    def find_idempotency(self, conn: sqlite3.Connection, actor_id: str, idem_key: str) -> Optional[Dict[str, Any]]:
        row = self._query_one(conn, "SELECT * FROM idempotency_keys WHERE actor_id=? AND idem_key=?", (actor_id, idem_key))
        if row is None:
            return None
        item = dict(row)
        item["response"] = json.loads(item["response"])
        return item

    def store_idempotency(self, conn: sqlite3.Connection, actor_id: str, idem_key: str, endpoint: str, fingerprint: str, response: Dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO idempotency_keys(actor_id,idem_key,endpoint,fingerprint,response,created_at) VALUES(?,?,?,?,?,?)",
            (actor_id, idem_key, endpoint, fingerprint, json.dumps(response, ensure_ascii=False, sort_keys=True), _now()),
        )

    # ---------- 审计与统计 ----------

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self.transaction() as conn:
            row = conn.execute("SELECT version FROM records WHERE id=?", (int(record_id),)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            conn.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (int(record_id), action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        rows = self._query_all(None, "SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (int(record_id),))
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self, organization: Optional[str] = None) -> Dict[str, int]:
        if organization is None:
            rows = self._query_all(None, "SELECT state, COUNT(*) AS total FROM records GROUP BY state", ())
        else:
            rows = self._query_all(None, "SELECT state, COUNT(*) AS total FROM records WHERE organization=? GROUP BY state", (organization,))
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
