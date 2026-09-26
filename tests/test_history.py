from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.history import read_page


class HistoryTests(unittest.TestCase):
    def test_real_message_schema_rowid_alias(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / "message"
            directory.mkdir()
            table = "Msg_" + hashlib.md5(b"wxid_one").hexdigest()
            with closing(sqlite3.connect(directory / "message_0.db")) as db, db:
                db.execute(f'CREATE TABLE "{table}"(local_id INTEGER PRIMARY KEY, '
                           'create_time INTEGER, message_content TEXT, real_sender_id INTEGER)')
                db.execute(f'INSERT INTO "{table}" VALUES (7,100,"hello",NULL)')
            rows, cursor, _ = read_page(root, "wxid_one")
            self.assertEqual(rows[0][2], 7)
            self.assertEqual(cursor[2], 7)

    def test_pages_only_confirmed_username_across_shards(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / "message"
            directory.mkdir()
            selected = "wxid_one"
            table = "Msg_" + hashlib.md5(selected.encode()).hexdigest()
            other = "Msg_" + hashlib.md5(b"wxid_other").hexdigest()
            for shard, values in enumerate(((10, 30), (20, 40))):
                with closing(sqlite3.connect(directory / f"message_{shard}.db")) as db, db:
                    db.execute(f'CREATE TABLE "{table}"(create_time INTEGER, message_content TEXT)')
                    db.execute(f'CREATE TABLE "{other}"(create_time INTEGER, message_content TEXT)')
                    db.executemany(f'INSERT INTO "{table}" VALUES (?,?)', [(v, f"m{v}") for v in values])
                    db.execute(f'INSERT INTO "{other}" VALUES (99,"private")')
            first, cursor, more = read_page(root, selected, limit=2)
            self.assertEqual([row[4] for row in first], ["m30", "m40"])
            self.assertTrue(more)
            second, _, more = read_page(root, selected, before=cursor, limit=2)
            self.assertEqual([row[4] for row in second], ["m10", "m20"])
            self.assertNotIn("private", [row[4] for row in first + second])


if __name__ == "__main__":
    unittest.main()
