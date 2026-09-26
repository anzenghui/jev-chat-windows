from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import accounts, db_snapshot


class SnapshotTests(unittest.TestCase):
    def test_prepare_uses_local_copy_and_never_sqlite_opens_source(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "wxid_a" / "db_storage"
            contact = root / "contact" / "contact.db"
            contact.parent.mkdir(parents=True)
            with closing(sqlite3.connect(contact)) as db, db:
                db.execute("CREATE TABLE contact(username TEXT)")
                db.execute("INSERT INTO contact VALUES ('wxid_test')")
            original = contact.read_bytes()
            app_root = base / "app"
            app_root.mkdir()
            real_connect = db_snapshot.sqlite3.connect
            opened = []

            def checked_connect(target, *args, **kwargs):
                opened.append(str(target))
                self.assertNotIn(str(root), str(target))
                return real_connect(target, *args, **kwargs)

            with patch.object(accounts, "__file__", str(app_root / "app" / "accounts.py")), \
                 patch.object(db_snapshot, "account_cache", side_effect=accounts.account_cache), \
                 patch.object(db_snapshot.sqlite3, "connect", side_effect=checked_connect):
                copied = db_snapshot.prepare_database(root)
                self.assertEqual(accounts.snapshot_for_account(root), copied)
                self.assertTrue(opened)
                self.assertEqual(contact.read_bytes(), original)
                with closing(real_connect(copied / "contact" / "contact.db")) as db:
                    self.assertEqual(db.execute("SELECT username FROM contact").fetchone()[0], "wxid_test")

    def test_plaintext_wal_is_merged_from_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "wxid_a" / "db_storage"
            source = root / "contact" / "contact.db"
            source.parent.mkdir(parents=True)
            with closing(sqlite3.connect(source)) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE contact(username TEXT)")
                db.commit()
                db.execute("INSERT INTO contact VALUES ('from_wal')")
                db.commit()
                original = source.read_bytes()
                with patch.object(db_snapshot, "account_cache", return_value=base / "cache"):
                    prepared = db_snapshot.prepare_database(root)
                with closing(sqlite3.connect(prepared / "contact" / "contact.db")) as copy:
                    self.assertEqual(copy.execute("SELECT username FROM contact").fetchone()[0], "from_wal")
                self.assertEqual(source.read_bytes(), original)

    def test_failed_prepare_does_not_publish_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "wxid_a" / "db_storage"
            source = root / "contact" / "contact.db"
            source.parent.mkdir(parents=True)
            source.write_bytes(bytes(4096))
            cache = base / "cache"
            with patch.object(db_snapshot, "account_cache", return_value=cache):
                with self.assertRaisesRegex(ValueError, "密钥"):
                    db_snapshot.prepare_database(root)
                self.assertFalse((cache / "ready.json").exists())
                self.assertEqual(source.read_bytes(), bytes(4096))


if __name__ == "__main__":
    unittest.main()
