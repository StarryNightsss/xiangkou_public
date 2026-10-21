"""Owner/session scoped slots with durable optimistic concurrency control."""
import copy
import asyncio
import weakref
import json
import time


class BusinessStateConflict(RuntimeError):
    pass


# 业务槽位按账号/会话隔离，30 分钟上下文期限与动作草稿的 5 分钟期限分开。
class BusinessStateStore:
    def __init__(self, database, ttl_seconds=1800, max_records=1000):
        self.database, self.ttl, self.limit = database, ttl_seconds, max_records
        self.records = {}
        self._locks = weakref.WeakValueDictionary()

    # 同进程同一会话串行处理；跨进程冲突仍由保存时的版本比较处理。
    def lock(self, owner, session):
        key = (owner, session)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def snapshot(self, owner, session):
        if not session:
            return None, 0
        if self.database:
            with self.database.connect() as conn:
                row = conn.execute('SELECT record_json FROM business_tasks WHERE owner_user_id=? AND session_id=? AND updated_at>=?',
                                   (owner, session, time.time() - self.ttl)).fetchone()
            record = json.loads(row[0]) if row else None
        else:
            row = self.records.get((owner, session))
            record = copy.deepcopy(row[0]) if row and row[1] >= time.time() - self.ttl else None
        if not record:
            return None, 0
        # 已删除记录仍返回版本号，防止旧请求以“没有状态”为由重新创建旧任务。
        revision = record.get('_revision', 0)
        return (None if record.get('_deleted') else record), revision

    def get(self, owner, session):
        return self.snapshot(owner, session)[0]

    def save(self, owner, session, record, expected_revision=None, connection=None):
        now = time.time()
        if self.database:
            # 调用方可传入动作事务连接，使槽位与草稿变更一起提交或回滚。
            if connection is not None:
                return self._save_sqlite(connection, owner, session, record, expected_revision, now)
            with self.database.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                return self._save_sqlite(conn, owner, session, record, expected_revision, now)
        else:
            _, revision = self.snapshot(owner, session)
            updated = self._updated(record, revision, expected_revision)
            self.records[(owner, session)] = (updated, now)
            self.records = dict(sorted(((key, value) for key, value in self.records.items() if value[1] >= now-self.ttl),
                                       key=lambda pair: pair[1][1], reverse=True)[:self.limit])
        return updated['_revision']

    def _save_sqlite(self, conn, owner, session, record, expected_revision, now):
        row = conn.execute('SELECT record_json FROM business_tasks WHERE owner_user_id=? AND session_id=? AND updated_at>=?',
                           (owner, session, now-self.ttl)).fetchone()
        revision = json.loads(row[0]).get('_revision', 0) if row else 0
        updated = self._updated(record, revision, expected_revision)
        conn.execute('DELETE FROM business_tasks WHERE updated_at<?', (now-self.ttl,))
        conn.execute('INSERT INTO business_tasks VALUES (?, ?, ?, ?) ON CONFLICT(owner_user_id,session_id) DO UPDATE SET record_json=excluded.record_json,updated_at=excluded.updated_at',
                     (owner, session, json.dumps(updated, ensure_ascii=False), now))
        conn.execute('DELETE FROM business_tasks WHERE rowid IN (SELECT rowid FROM business_tasks ORDER BY updated_at DESC LIMIT -1 OFFSET ?)', (self.limit,))
        return updated['_revision']

    @staticmethod
    def _updated(record, revision, expected):
        # 乐观并发校验：只允许基于当前版本更新，不默默覆盖较新的选择。
        if expected is not None and expected != revision:
            raise BusinessStateConflict('业务会话已更新，请按最新结果重试')
        return {**copy.deepcopy(record), '_revision': revision + 1}

    def clear(self, owner, session, expected_revision=None):
        if not session:
            return 0
        # A tombstone prevents stale requests resurrecting a cleared task.
        return self.save(owner, session, {'_deleted': True}, expected_revision)
