"""SQLite 表结构与事务访问。

并发约束全部落在数据库事务里：
- 案件动作：records.version 乐观锁，晚到的书记员收到版本冲突。
- 受理：先锁定全局当前政策版本指针再插入，期间若管理员发布了新版本则冲突，
  保证不会出现“按旧政策写快照、材料却参照新版本”的半成品。
- 发布：BEGIN IMMEDIATE 后对当前政策版本做 CAS，并在同一事务内重算所有草稿。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .domain import Conflict, NotFound
from .rules import DomainRules


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str, rules: Optional[DomainRules] = None) -> None:
        self.db_path = db_path
        self.rules = rules or DomainRules()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS policy_versions (
                    version INTEGER PRIMARY KEY,
                    content TEXT NOT NULL,
                    published_by TEXT NOT NULL,
                    source_version INTEGER,
                    rollback INTEGER NOT NULL DEFAULT 0,
                    published_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    org TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    policy_version INTEGER NOT NULL,
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
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_records_org ON records(org);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                """
            )
            seeded = connection.execute("SELECT value FROM app_state WHERE key='current_policy_version'").fetchone()
            if seeded is None:
                now = _now()
                seed_content = self.rules.seed_content()
                connection.execute(
                    "INSERT INTO policy_versions(version,content,published_by,published_at) VALUES(?,?,?,?)",
                    (1, json.dumps(seed_content, ensure_ascii=False, sort_keys=True), "system", now),
                )
                connection.execute(
                    "INSERT INTO app_state(key,value) VALUES('current_policy_version','1')"
                )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def _policy_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "version": int(row["version"]),
            "content": json.loads(row["content"]),
            "published_by": row["published_by"],
            "source_version": None if row["source_version"] is None else int(row["source_version"]),
            "rollback": bool(row["rollback"]),
            "published_at": row["published_at"],
        }

    # ---- 政策版本 ----
    def current_policy(self) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT p.* FROM policy_versions p
                JOIN app_state s ON s.value = CAST(p.version AS TEXT)
                WHERE s.key='current_policy_version'
                """
            ).fetchone()
        if row is None:
            raise NotFound("当前政策版本不存在")
        return self._policy_row(row)

    def get_policy(self, version: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM policy_versions WHERE version=?", (version,)).fetchone()
        if row is None:
            raise NotFound("政策版本不存在")
        return self._policy_row(row)

    def list_policies(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM policy_versions ORDER BY version").fetchall()
        return [self._policy_row(row) for row in rows]

    def publish_policy(
        self,
        expected_version: int,
        content: Dict[str, Any],
        actor_id: str,
        source_version: Optional[int] = None,
        rollback: bool = False,
        rebase_audit: Callable[[sqlite3.Connection, int, Dict[str, Any], Dict[str, Any]], int] = None,
    ) -> Dict[str, Any]:
        """追加一个政策版本，并在同一事务内把所有草稿重算到新版本。

        回滚也只追加新版本（复制历史内容），历史版本行永不修改。
        """
        now = _now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = int(
                connection.execute("SELECT value FROM app_state WHERE key='current_policy_version'").fetchone()["value"]
            )
            if current != int(expected_version):
                connection.rollback()
                raise Conflict("政策版本冲突：当前已发布到v%s，请基于新版本重试" % current)
            if source_version is not None:
                source_row = connection.execute(
                    "SELECT * FROM policy_versions WHERE version=?", (int(source_version),)
                ).fetchone()
                if source_row is None:
                    connection.rollback()
                    raise NotFound("回滚源版本不存在")
            new_version = current + 1
            content_json = json.dumps(content, ensure_ascii=False, sort_keys=True)
            connection.execute(
                "INSERT INTO policy_versions(version,content,published_by,source_version,rollback,published_at) VALUES(?,?,?,?,?,?)",
                (new_version, content_json, actor_id, source_version, 1 if rollback else 0, now),
            )
            connection.execute(
                "UPDATE app_state SET value=? WHERE key='current_policy_version'", (str(new_version),)
            )
            # 同事务内重算所有草稿：快照和材料清单一起切换，不会只写一半。
            rebased: List[Dict[str, Any]] = []
            draft_rows = connection.execute(
                "SELECT * FROM records WHERE state=? ORDER BY id", (self.rules.INITIAL_STATE,)
            ).fetchall()
            for draft_row in draft_rows:
                record = self._row(draft_row)
                new_payload = self.rules.rebase_draft(record["payload"], new_version, content)
                connection.execute(
                    "UPDATE records SET policy_version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                    (new_version, json.dumps(new_payload, ensure_ascii=False, sort_keys=True), actor_id, now, record["id"]),
                )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        record["id"],
                        "policy_rebased",
                        actor_id,
                        record["version"],
                        json.dumps(
                            {"from_version": current, "to_version": new_version, "rollback": rollback},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
                rebased.append({"record_id": record["id"], "reference": record["reference"]})
            if rebase_audit is not None:
                rebase_audit(connection, new_version, content, {"rebased": rebased, "rollback": rollback})
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_policy(new_version)

    # ---- 受理（快照当前政策） ----
    def create(
        self,
        reference: str,
        org: str,
        payload: Dict[str, Any],
        actor_id: str,
        expected_policy_version: Optional[int] = None,
        policy_load_hook: Callable[[], None] = None,
    ) -> Dict[str, Any]:
        """按当前政策快照受理新案件。

        先锁定全局政策指针，确保受理过程中若管理员发布了新版本，本次受理整体失败，
        而不是留下政策快照与材料清单各写一半的案件。
        """
        now = _now()
        if policy_load_hook is not None:
            # 测试钩子：模拟书记员锁前读到当前政策后、写入锁前管理员发布新版本的撞车窗口。
            # 锁内会再次核对全局版本指针，因此发布先到时本次受理整体失败。
            policy_load_hook()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current_row = connection.execute(
                "SELECT value FROM app_state WHERE key='current_policy_version'"
            ).fetchone()
            current_version = int(current_row["value"])
            if expected_policy_version is not None and current_version != int(expected_policy_version):
                connection.rollback()
                raise Conflict(
                    "受理与新版本发布相撞：当前政策已为v%s，请按新版本重新受理" % current_version
                )
            policy_row = connection.execute(
                "SELECT * FROM policy_versions WHERE version=?", (current_version,)
            ).fetchone()
            # 若锁外缓存的政策已过时，按锁内政策整体重算，快照与材料清单永远取自同一版本。
            if int(policy_row["version"]) != int(payload["policy"]["version"]):
                payload = self.rules.rebase_draft(payload, current_version, json.loads(policy_row["content"]))
            cursor = connection.execute(
                "INSERT INTO records(reference,org,state,version,policy_version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    reference,
                    org,
                    self.rules.INITIAL_STATE,
                    1,
                    current_version,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    actor_id,
                    actor_id,
                    now,
                    now,
                ),
            )
            record_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (
                    record_id,
                    "created",
                    actor_id,
                    1,
                    json.dumps({"state": self.rules.INITIAL_STATE, "policy_version": current_version}, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise Conflict("reference已存在") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, org: Optional[str] = None, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if org is not None and state:
                rows = connection.execute(
                    "SELECT * FROM records WHERE org=? AND state=? ORDER BY id DESC LIMIT ?", (org, state, limit)
                ).fetchall()
            elif org is not None:
                rows = connection.execute(
                    "SELECT * FROM records WHERE org=? ORDER BY id DESC LIMIT ?", (org, limit)
                ).fetchall()
            elif state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突：记录已被他人更新（当前v%s），请刷新后重试" % row["version"])
            version = int(expected_version) + 1
            # 政策快照版本不随动作改变：提交后永远认受理时的版本。
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
