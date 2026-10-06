import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from .domain import ConflictError, NotFoundError

SYSTEM_ACTOR = "system"
SCHEMA_VERSION = 2


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                """
            )
            self._migrate(connection)

    # ------------------------------------------------------------------ schema
    def _migrate(self, connection):
        version = int(connection.execute("PRAGMA user_version").fetchone()[0] or 0)
        if version >= SCHEMA_VERSION:
            return
        if version < 2:
            self._migrate_v2(connection)
        connection.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)

    def _migrate_v2(self, connection):
        # 停车排程账：每个停车窗口一条 FIFO 队列，申请顺序即 seq
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schedule_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                window_id TEXT NOT NULL,
                booking_id TEXT NOT NULL,
                change_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                state TEXT NOT NULL,
                idem_key TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(window_id, idem_key),
                UNIQUE(window_id, seq)
            );
            CREATE INDEX IF NOT EXISTS idx_ledger_window_state
                ON schedule_ledger(window_id, state, seq);
            CREATE INDEX IF NOT EXISTS idx_ledger_booking
                ON schedule_ledger(booking_id);
            """
        )
        # 旧数据没有落实状态：按已有复核记录回填，历史记录原样保留仍可打开
        rows = connection.execute("SELECT * FROM entities").fetchall()
        items_by_change = {}
        for item_row in rows:
            if item_row["kind"] == "action_item":
                data = json.loads(item_row["data"])
                if "review" not in data:
                    data["review"] = {
                        "result": "verified" if item_row["status"] == "verified" else "outstanding",
                        "source": "backfill",
                    }
                    self._update_row_data(connection, item_row, data)
                items_by_change.setdefault(data.get("change_id"), []).append(item_row)
        for row in rows:
            if row["kind"] != "change":
                continue
            data = json.loads(row["data"])
            dirty = False
            if "implementation_state" not in data:
                items = items_by_change.get(row["id"], [])
                all_verified = bool(items) and all(
                    item["status"] == "verified" for item in items
                )
                if row["status"] in ("implemented", "commissioned", "rolled_back", "closed"):
                    data["implementation_state"] = "fulfilled"
                elif row["status"] == "approved" and all_verified:
                    data["implementation_state"] = "ready"
                else:
                    data["implementation_state"] = "pending"
                dirty = True
            if "required_measures" not in data:
                from .rules import required_measures

                data["required_measures"] = (
                    required_measures(data.get("risk_level"))
                    if data.get("risk_level")
                    else []
                )
                dirty = True
            if dirty:
                data["backfilled_by"] = "schema-v2"
                self._update_row_data(connection, row, data)
                connection.execute(
                    "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, "
                    "from_status, to_status, detail, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        row["id"],
                        SYSTEM_ACTOR,
                        "system",
                        "migration_backfill",
                        row["status"],
                        row["status"],
                        json.dumps({"schema": 2}, sort_keys=True),
                        utcnow(),
                    ),
                )

    @staticmethod
    def _update_row_data(connection, row, data):
        connection.execute(
            "UPDATE entities SET data = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False, sort_keys=True), row["id"]),
        )

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ----------------------------------------------------------------- entities
    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def update_entity_raw(self, entity_id, status, data):
        """系统级联写：改数据/状态并推进版本，不做乐观锁校验。"""
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            cur = connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ?",
                (status, payload, now, entity_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("entity not found: " + entity_id)
        return self.get_entity(entity_id)

    # ------------------------------------------------------------------ ledger
    @staticmethod
    def _overlap(start_a, end_a, start_b, end_b):
        # 半开区间：[start, end)
        return start_a < end_b and start_b < end_a

    def _window_row(self, connection, window_id):
        row = connection.execute(
            "SELECT * FROM entities WHERE id = ? AND kind = 'window'", (window_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("window not found: " + window_id)
        return row

    def _occupancy_count(self, connection, window_id, seg_start, seg_end):
        rows = connection.execute(
            "SELECT e.data FROM schedule_ledger l JOIN entities e ON e.id = l.booking_id "
            "WHERE l.window_id = ? AND l.state IN ('scheduled', 'consumed')",
            (window_id,),
        ).fetchall()
        count = 0
        for row in rows:
            data = json.loads(row["data"])
            if self._overlap(
                seg_start,
                seg_end,
                datetime.fromisoformat(data["segment_start"]),
                datetime.fromisoformat(data["segment_end"]),
            ):
                count += 1
        return count

    def window_has_room(self, window_id, seg_start, seg_end):
        """取写锁前的只读快照看容量；用于区分排队与并发抢锁失败。"""
        with self._connect() as connection:
            row = self._window_row(connection, window_id)
            capacity = int(json.loads(row["data"])["capacity"])
            return self._occupancy_count(connection, window_id, seg_start, seg_end) < capacity

    def get_ledger_idem(self, window_id, idem_key):
        if not idem_key:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT booking_id FROM schedule_ledger WHERE window_id = ? AND idem_key = ?",
                (window_id, idem_key),
            ).fetchone()
        return row["booking_id"] if row else None

    def submit_booking(
        self, window_id, change_id, seg_start, seg_end, actor_id, idem_key, fit_before
    ):
        """在一个写事务里完成：登记申请 -> 容量判定 -> 占位或排队。

        fit_before 为取写锁前的只读判断。若进锁后容量被并发申请占满，
        说明这是两名工程师同时抢同一窗口：晚到一方直接冲突，不进队列。
        """
        booking_id = str(uuid4())
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            window = self._window_row(connection, window_id)
            if window["status"] != "open":
                raise ConflictError("window is not open: " + window_id)
            if idem_key:
                dup = connection.execute(
                    "SELECT booking_id FROM schedule_ledger WHERE window_id = ? AND idem_key = ?",
                    (window_id, idem_key),
                ).fetchone()
                if dup:
                    booking = self._entity_from_row(
                        connection.execute(
                            "SELECT * FROM entities WHERE id = ?", (dup["booking_id"],)
                        ).fetchone()
                    )
                    connection.commit()
                    return booking, "existing"
            fits = (
                self._occupancy_count(connection, window_id, seg_start, seg_end)
                < int(json.loads(window["data"])["capacity"])
            )
            if not fits and fit_before:
                raise ConflictError(
                    "window %s was claimed by a concurrent change; retry to join the queue"
                    % window_id
                )
            state = "scheduled" if fits else "queued"
            seq_row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM schedule_ledger WHERE window_id = ?",
                (window_id,),
            ).fetchone()
            seq = int(seq_row["next_seq"])
            data = {
                "window_id": window_id,
                "change_id": change_id,
                "unit_id": json.loads(window["data"])["unit_id"],
                "segment_start": seg_start.isoformat(),
                "segment_end": seg_end.isoformat(),
                "seq": seq,
            }
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, 'booking', ?, 1, ?, ?, ?, ?)",
                (
                    booking_id,
                    state,
                    json.dumps(data, ensure_ascii=False, sort_keys=True),
                    actor_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO schedule_ledger(window_id, booking_id, change_id, seq, state, idem_key, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (window_id, booking_id, change_id, seq, state, idem_key, now, now),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(booking_id), state

    def _promote_queue(self, connection, window_id, now):
        """FIFO 恢复：按申请顺序逐个尝试占位，遇到放不下的队首即停止。"""
        window = self._window_row(connection, window_id)
        capacity = int(json.loads(window["data"])["capacity"])
        promoted = []
        rows = connection.execute(
            "SELECT * FROM schedule_ledger WHERE window_id = ? AND state = 'queued' ORDER BY seq",
            (window_id,),
        ).fetchall()
        for ledger in rows:
            booking = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (ledger["booking_id"],)
            ).fetchone()
            data = json.loads(booking["data"])
            seg_start = datetime.fromisoformat(data["segment_start"])
            seg_end = datetime.fromisoformat(data["segment_end"])
            if self._occupancy_count(connection, window_id, seg_start, seg_end) >= capacity:
                break
            connection.execute(
                "UPDATE entities SET status = 'scheduled', version = version + 1, updated_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (now, ledger["booking_id"]),
            )
            connection.execute(
                "UPDATE schedule_ledger SET state = 'scheduled', updated_at = ? WHERE id = ?",
                (now, ledger["id"]),
            )
            promoted.append(ledger["booking_id"])
        return promoted

    def recover_window(self, window_id):
        """排程写入失败后的恢复入口：只把已登记、未占位的排队申请按顺序重试。"""
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            promoted = self._promote_queue(connection, window_id, now)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return [self.get_entity(booking_id) for booking_id in promoted]

    def list_open_window_ids(self):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM entities WHERE kind = 'window' AND status = 'open'"
            ).fetchall()
        return [row["id"] for row in rows]

    def release_booking(self, booking_id, reason=None):
        now = utcnow()
        connection = self._connect()
        window_id = None
        freed = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ? AND kind = 'booking'", (booking_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("booking not found: " + booking_id)
            if row["status"] not in ("queued", "scheduled", "consumed"):
                raise ConflictError("booking already %s" % row["status"])
            window_id = json.loads(row["data"])["window_id"]
            freed = row["status"] in ("scheduled", "consumed")
            connection.execute(
                "UPDATE entities SET status = 'released', version = version + 1, updated_at = ? WHERE id = ?",
                (now, booking_id),
            )
            connection.execute(
                "UPDATE schedule_ledger SET state = 'released', updated_at = ? WHERE booking_id = ?",
                (now, booking_id),
            )
            promoted = self._promote_queue(connection, window_id, now) if freed else []
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        booking = self.get_entity(booking_id)
        promoted_entities = [self.get_entity(bid) for bid in promoted]
        return booking, promoted_entities

    def consume_booking_for_change(self, change_id):
        now = utcnow()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT l.booking_id FROM schedule_ledger l JOIN entities e ON e.id = l.booking_id "
                "WHERE l.change_id = ? AND l.state = 'scheduled'",
                (change_id,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE entities SET status = 'consumed', version = version + 1, updated_at = ? "
                    "WHERE id = ?",
                    (now, row["booking_id"]),
                )
                connection.execute(
                    "UPDATE schedule_ledger SET state = 'consumed', updated_at = ? WHERE booking_id = ?",
                    (now, row["booking_id"]),
                )
            return [row["booking_id"] for row in rows]

    def release_bookings_for_change(self, change_id):
        """变更投产/回退离开窗口：释放占用并按顺序恢复排队。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT booking_id, window_id FROM schedule_ledger "
                "WHERE change_id = ? AND state IN ('scheduled', 'consumed')",
                (change_id,),
            ).fetchall()
            window_ids = [row["window_id"] for row in rows]
        promoted_all = []
        for row in rows:
            _, promoted = self.release_booking(row["booking_id"], reason="change-left-window")
            promoted_all.extend(promoted)
        return window_ids, promoted_all

    def get_scheduled_booking(self, change_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT booking_id FROM schedule_ledger WHERE change_id = ? AND state = 'scheduled'",
                (change_id,),
            ).fetchone()
        return self.get_entity(row["booking_id"]) if row else None

    def get_window_schedule(self, window_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT b.* FROM schedule_ledger l JOIN entities b ON b.id = l.booking_id "
                "WHERE l.window_id = ? ORDER BY l.seq",
                (window_id,),
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    # -------------------------------------------------------------------- audit
    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------- idempotency
    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
