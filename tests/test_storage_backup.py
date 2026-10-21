from xiangkou.storage import StateDatabase


def test_backup_preserves_committed_state_and_schema_version(tmp_path):
    source = StateDatabase(str(tmp_path / "live.sqlite3"))
    with source.connect() as conn:
        conn.execute("INSERT INTO user_memory VALUES (?, ?, ?)", (7, '{"area":"运河上街"}', 1.0))
    destination = source.backup(str(tmp_path / "backup.sqlite3"))
    copy = StateDatabase(destination)
    assert copy.check()
    with copy.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("SELECT preferences_json FROM user_memory WHERE owner_user_id=7").fetchone()[0] == '{"area":"运河上街"}'
