"""Durable read-workflow checkpoints; business writes stay in VoucherActionWorkflow."""
import time


# 保存只读工作流计划、阶段和会话版本；交易动作另存，恢复这里不会自动提交订单。
class CheckpointStore:
    def __init__(self, database, max_records=1000):
        self.database = database
        self.max_records = max_records
        self.records = {}

    def save(self, run_id, user_id, session_id, plan, revision):
        record = dict(run_id=run_id, owner_user_id=user_id, session_id=session_id,
                      plan_json=plan.model_dump_json(), revision=revision, phase="planned", updated_at=time.time())
        if self.database:
            with self.database.connect() as conn:
                conn.execute("DELETE FROM task_checkpoints WHERE updated_at < ?", (time.time() - 30 * 86400,))
                conn.execute("INSERT OR REPLACE INTO task_checkpoints VALUES (?, ?, ?, ?, ?, ?, ?)", tuple(record.values()))
                conn.execute("DELETE FROM task_checkpoints WHERE run_id IN (SELECT run_id FROM task_checkpoints ORDER BY updated_at DESC LIMIT -1 OFFSET ?)", (self.max_records,))
        else:
            self.records[run_id] = record
            self.records = dict(sorted(self.records.items(), key=lambda pair: pair[1]["updated_at"], reverse=True)[:self.max_records])

    def phase(self, run_id, phase, plan=None):
        if self.database:
            with self.database.connect() as conn:
                conn.execute("UPDATE task_checkpoints SET phase=?, updated_at=? WHERE run_id=?", (phase, time.time(), run_id))
                if plan is not None:
                    conn.execute("UPDATE task_checkpoints SET plan_json=? WHERE run_id=?", (plan.model_dump_json(), run_id))
        elif run_id in self.records:
            self.records[run_id]["phase"] = phase
            if plan is not None:
                self.records[run_id]["plan_json"] = plan.model_dump_json()

    def get(self, run_id, user_id):
        if self.database:
            with self.database.connect() as conn:
                row = conn.execute("SELECT * FROM task_checkpoints WHERE run_id=? AND owner_user_id IS ? "
                                   "AND updated_at>=?", (run_id, user_id, time.time() - 30 * 86400)).fetchone()
            return dict(row) if row else None
        record = self.records.get(run_id)
        return dict(record) if record and record["owner_user_id"] == user_id else None

    def recent(self, user_id):
        if self.database:
            with self.database.connect() as conn:
                rows = conn.execute("SELECT run_id, session_id, phase, updated_at FROM task_checkpoints "
                                    "WHERE owner_user_id=? AND updated_at>=? ORDER BY updated_at DESC LIMIT 20",
                                    (user_id, time.time() - 30 * 86400)).fetchall()
            return [dict(row) for row in rows]
        return [{key: record[key] for key in ("run_id", "session_id", "phase", "updated_at")}
                for record in self.records.values() if record["owner_user_id"] == user_id][-20:]
