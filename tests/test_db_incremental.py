from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import accounts, db_incremental, db_snapshot


class IncrementalTests(unittest.TestCase):
    def test_legacy_contact_refreshes_from_copy_and_keeps_source_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "wxid_a" / "db_storage"
            contact = root / "contact" / "contact.db"
            contact.parent.mkdir(parents=True)
            app_root = base / "app"
            app_root.mkdir()
            with closing(sqlite3.connect(contact)) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE contact(username TEXT, nick_name TEXT)")
                db.execute("INSERT INTO contact VALUES ('old@openim', '旧联系人')")
                db.commit()
                with patch.object(accounts, "__file__", str(app_root / "app" / "accounts.py")):
                    snapshot = db_snapshot.prepare_database(root)
                    (snapshot / "sync.json").unlink()
                    self.assertTrue(db_incremental.refresh_legacy_contact(root, snapshot))
                    self.assertFalse(db_incremental.refresh_legacy_contact(root, snapshot))
                    db.execute("INSERT INTO contact VALUES ('new@openim', '新联系人')")
                    db.commit()
                    source_before = contact.read_bytes()
                    self.assertTrue(db_incremental.refresh_legacy_contact(root, snapshot))
                    self.assertEqual(contact.read_bytes(), source_before)
                    with closing(sqlite3.connect((snapshot / "contact" / "contact.db").as_uri() + "?mode=ro", uri=True)) as copy:
                        self.assertEqual(copy.execute("SELECT nick_name FROM contact WHERE username='new@openim'").fetchone()[0], "新联系人")

    def test_legacy_snapshot_refreshes_only_session_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "wxid_a" / "db_storage"
            contact = root / "contact" / "contact.db"
            contact.parent.mkdir(parents=True)
            with closing(sqlite3.connect(contact)) as db, db:
                db.execute("CREATE TABLE contact(username TEXT)")
            app_root = base / "app"
            app_root.mkdir()
            with patch.object(accounts, "__file__", str(app_root / "app" / "accounts.py")):
                snapshot = db_snapshot.prepare_database(root)
                (snapshot / "sync.json").unlink()
                session = root / "session" / "session.db"
                session.parent.mkdir()
                with closing(sqlite3.connect(session)) as db:
                    db.execute("PRAGMA journal_mode=WAL")
                    db.execute("CREATE TABLE SessionTable(username TEXT, last_clear_unread_timestamp INTEGER)")
                    db.execute("INSERT INTO SessionTable VALUES ('wxid_a', 10)")
                    db.commit()
                    source_before = session.read_bytes()
                    self.assertTrue(db_incremental.refresh_legacy_session(root, snapshot))
                    self.assertFalse(db_incremental.refresh_legacy_session(root, snapshot))
                    db.execute("UPDATE SessionTable SET last_clear_unread_timestamp=11")
                    db.commit()
                    self.assertTrue(db_incremental.refresh_legacy_session(root, snapshot))
                    self.assertEqual(session.read_bytes(), source_before)
                with closing(sqlite3.connect((snapshot / "session" / "session.db").as_uri() + "?mode=ro", uri=True)) as copy:
                    self.assertEqual(copy.execute("SELECT last_clear_unread_timestamp FROM SessionTable").fetchone()[0], 11)

    def test_existing_snapshot_adds_and_syncs_session_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "wxid_a" / "db_storage"
            contact = root / "contact" / "contact.db"
            contact.parent.mkdir(parents=True)
            with closing(sqlite3.connect(contact)) as db, db:
                db.execute("CREATE TABLE contact(username TEXT)")
            app_root = base / "app"
            app_root.mkdir()
            with patch.object(accounts, "__file__", str(app_root / "app" / "accounts.py")):
                snapshot = db_snapshot.prepare_database(root)
                session = root / "session" / "session.db"
                session.parent.mkdir()
                with closing(sqlite3.connect(session)) as db:
                    db.execute("PRAGMA journal_mode=WAL")
                    db.execute("CREATE TABLE SessionTable(username TEXT, last_clear_unread_timestamp INTEGER)")
                    db.execute("INSERT INTO SessionTable VALUES ('wxid_a', 10)")
                    db.commit()
                    before = session.read_bytes()
                    changed, rebased = db_incremental.sync_account(root, snapshot)
                    self.assertEqual(changed, [])
                    self.assertFalse(rebased)
                    self.assertIn("session/session.db", json.loads((snapshot / "sync.json").read_text()))
                    db.execute("UPDATE SessionTable SET last_clear_unread_timestamp=11")
                    db.commit()
                    changed, rebased = db_incremental.sync_account(root, snapshot)
                    self.assertEqual(changed, ["session/session.db"])
                    self.assertFalse(rebased)
                    self.assertEqual(session.read_bytes(), before)
                    with closing(sqlite3.connect((snapshot / "session" / "session.db").as_uri() + "?mode=ro", uri=True)) as copy:
                        self.assertEqual(copy.execute("SELECT last_clear_unread_timestamp FROM SessionTable").fetchone()[0], 11)

    def test_append_and_wal_reset_update_only_local_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "wxid_a" / "db_storage"
            source = root / "contact" / "contact.db"
            source.parent.mkdir(parents=True)
            app_root = base / "app"
            app_root.mkdir()
            with closing(sqlite3.connect(source)) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE contact(username TEXT)")
                db.commit()
                with patch.object(accounts, "__file__", str(app_root / "app" / "accounts.py")):
                    snapshot = db_snapshot.prepare_database(root)
                    db.execute("INSERT INTO contact VALUES ('first')")
                    db.commit()
                    source_bytes = source.read_bytes()
                    changed, rebased = db_incremental.sync_account(root, snapshot)
                    self.assertEqual(changed, ["contact/contact.db"])
                    self.assertFalse(rebased)
                    self.assertEqual(source.read_bytes(), source_bytes)
                    with closing(sqlite3.connect((snapshot / "contact" / "contact.db").as_uri() + "?mode=ro", uri=True)) as copy:
                        self.assertEqual(copy.execute("SELECT username FROM contact").fetchall(), [("first",)])
                    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    db.execute("INSERT INTO contact VALUES ('second')")
                    db.commit()
                    changed, rebased = db_incremental.sync_account(root, snapshot)
                    self.assertEqual(changed, ["contact/contact.db"])
                    self.assertTrue(rebased)
                    with closing(sqlite3.connect((snapshot / "contact" / "contact.db").as_uri() + "?mode=ro", uri=True)) as copy:
                        self.assertEqual(copy.execute("SELECT username FROM contact ORDER BY username").fetchall(),
                                         [("first",), ("second",)])

    def test_uncommitted_page_patch_recovers_before_read(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "wxid_a" / "db_storage"
            source = root / "contact" / "contact.db"
            source.parent.mkdir(parents=True)
            with closing(sqlite3.connect(source)) as db, db:
                db.execute("CREATE TABLE contact(username TEXT)")
            with patch.object(db_snapshot, "account_cache", return_value=base / "cache"):
                snapshot = db_snapshot.prepare_database(root)
            target = snapshot / "contact" / "contact.db"
            original = target.read_bytes()
            db_incremental._write_undo(target, {1: bytes(db_incremental.PAGE)}, bytes(32))
            with target.open("rb+") as stream:
                stream.write(bytes(db_incremental.PAGE))
            db_incremental.recover_pending(target)
            self.assertEqual(target.read_bytes(), original)
            self.assertFalse(Path(str(target) + ".undo").exists())

    def test_new_wal_with_unchanged_source_base_is_incremental(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "wxid_a" / "db_storage"
            source = root / "contact" / "contact.db"
            source.parent.mkdir(parents=True)
            with closing(sqlite3.connect(source)) as db, db:
                db.execute("CREATE TABLE contact(username TEXT)")
            app_root = base / "app"
            app_root.mkdir()
            with patch.object(accounts, "__file__", str(app_root / "app" / "accounts.py")):
                snapshot = db_snapshot.prepare_database(root)
                modified = base / "modified.db"
                import shutil
                shutil.copyfile(source, modified)
                with closing(sqlite3.connect(modified)) as db:
                    db.execute("PRAGMA journal_mode=WAL")
                    db.execute("PRAGMA user_version=7")
                    db.commit()
                    shutil.copyfile(Path(str(modified) + "-wal"), Path(str(source) + "-wal"))
                    changed, rebased = db_incremental.sync_account(root, snapshot)
                self.assertEqual(changed, ["contact/contact.db"])
                self.assertFalse(rebased)
                with closing(sqlite3.connect((snapshot / "contact" / "contact.db").as_uri() + "?mode=ro", uri=True)) as copy:
                    self.assertEqual(copy.execute("PRAGMA user_version").fetchone()[0], 7)

    def test_confirmed_chat_updates_only_its_message_shard(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "wxid_a" / "db_storage"
            contact = root / "contact" / "contact.db"
            contact.parent.mkdir(parents=True)
            with closing(sqlite3.connect(contact)) as db, db:
                db.execute("CREATE TABLE contact(username TEXT)")
            message_dir = root / "message"
            message_dir.mkdir()
            connections = []
            try:
                for number, username in enumerate(("wxid_one", "wxid_two")):
                    db = sqlite3.connect(message_dir / f"message_{number}.db")
                    connections.append(db)
                    db.execute("PRAGMA journal_mode=WAL")
                    table = "Msg_" + hashlib.md5(username.encode()).hexdigest()
                    db.execute(f'CREATE TABLE "{table}"(create_time INTEGER,message_content TEXT)')
                    db.commit()
                app_root = base / "app"
                app_root.mkdir()
                with patch.object(accounts, "__file__", str(app_root / "app" / "accounts.py")):
                    snapshot = db_snapshot.prepare_database(root)
                    for db in connections:
                        db.execute("PRAGMA user_version=9")
                        db.commit()
                    changed, rebased = db_incremental.sync_account(root, snapshot, username="wxid_one")
                    self.assertEqual(changed, ["message/message_0.db"])
                    self.assertFalse(rebased)
                    with closing(sqlite3.connect((snapshot / "message" / "message_1.db").as_uri() + "?mode=ro", uri=True)) as copy:
                        self.assertEqual(copy.execute("PRAGMA user_version").fetchone()[0], 0)
            finally:
                for db in connections:
                    db.close()


if __name__ == "__main__":
    unittest.main()
