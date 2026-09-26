"""Small plaintext fixtures exercise identity decisions without a running WeChat."""
import hashlib
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from app.plugin_loader import load_plugin

plugin = load_plugin("session_recognition", "recognizer")
Resolver, SessionSignal = plugin.Resolver, plugin.SessionSignal


class RecognitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "contact").mkdir()
        (self.root / "message").mkdir()
        with closing(sqlite3.connect(self.root / "contact" / "contact.db")) as db, db:
            db.execute("CREATE TABLE contact(username TEXT, nick_name TEXT, remark TEXT, alias TEXT)")
            db.executemany("INSERT INTO contact VALUES (?,?,?,?)", [
                ("wxid_a", "张三", "", ""),
                ("wxid_b", "张三", "", ""),
                ("wxid_c", "李四", "", ""),
            ])

    def messages(self, username, *texts):
        table = "Msg_" + hashlib.md5(username.encode()).hexdigest()
        with closing(sqlite3.connect(self.root / "message" / "message_0.db")) as db, db:
            db.execute(f'CREATE TABLE "{table}"(create_time INTEGER, message_content TEXT)')
            db.executemany(f'INSERT INTO "{table}" VALUES (?,?)', enumerate(texts, 1))

    def test_unique_title_confirms_contact_id(self):
        match = Resolver(self.root).resolve("李四")
        self.assertTrue(match.confirmed)
        self.assertEqual(match.username, "wxid_c")
        self.assertFalse(Resolver(self.root).resolve("李四", recent_username="wxid_a").confirmed)

    def test_manual_options_include_only_matching_duplicate_ids(self):
        resolver = Resolver(self.root)
        self.assertEqual({user for user, _ in resolver.candidate_options("张三")},
                         {"wxid_a", "wxid_b"})
        self.assertEqual(resolver.candidate_options("李四"), [])
        self.assertEqual(resolver.candidate_options("不存在"), [])

    def test_enterprise_contact_uses_openim_id_but_duplicate_name_stays_unconfirmed(self):
        with closing(sqlite3.connect(self.root / "contact" / "contact.db")) as db, db:
            db.execute("INSERT INTO contact VALUES (?,?,?,?)",
                       ("enterprise_a@openim", "企业联系人", "", ""))
        self.assertEqual(Resolver(self.root).resolve("企业联系人").username,
                         "enterprise_a@openim")
        with closing(sqlite3.connect(self.root / "contact" / "contact.db")) as db, db:
            db.execute("INSERT INTO contact VALUES (?,?,?,?)",
                       ("enterprise_b@openim", "企业联系人", "", ""))
        self.assertFalse(Resolver(self.root).resolve("企业联系人").confirmed)

    def test_keycap_digit_title_matches_only_when_unique(self):
        with closing(sqlite3.connect(self.root / "contact" / "contact.db")) as db, db:
            db.execute("INSERT INTO contact VALUES (?,?,?,?)",
                       ("group_a@chatroom", "山禾FDE交流2\ufe0f\u20e3群", "", ""))
        self.assertEqual(Resolver(self.root).resolve("山禾FDE交流2群").username,
                         "group_a@chatroom")

        with closing(sqlite3.connect(self.root / "contact" / "contact.db")) as db, db:
            db.execute("INSERT INTO contact VALUES (?,?,?,?)",
                       ("group_b@chatroom", "山禾FDE交流2群", "", ""))
        self.assertFalse(Resolver(self.root).resolve("山禾FDE交流2群").confirmed)

    def test_trailing_emoji_omitted_by_ocr_requires_unique_contact(self):
        with closing(sqlite3.connect(self.root / "contact" / "contact.db")) as db, db:
            db.execute("INSERT INTO contact VALUES (?,?,?,?)",
                       ("wxid_emoji", "仙女宝宝💗💗", "", ""))
            db.execute("INSERT INTO contact VALUES (?,?,?,?)",
                       ("wxid_other", "仙女宝宝老师", "", ""))
        self.assertEqual(Resolver(self.root).resolve("仙女宝宝").username, "wxid_emoji")
        self.assertFalse(Resolver(self.root).resolve("仙女宝").confirmed)

        with closing(sqlite3.connect(self.root / "contact" / "contact.db")) as db, db:
            db.execute("INSERT INTO contact VALUES (?,?,?,?)",
                       ("wxid_plain", "仙女宝宝", "", ""))
        self.assertFalse(Resolver(self.root).resolve("仙女宝宝").confirmed)

    def test_ocr_l_confusion_requires_matching_unique_group_size(self):
        with closing(sqlite3.connect(self.root / "contact" / "contact.db")) as db, db:
            db.execute("CREATE TABLE chat_room(id INTEGER, username TEXT)")
            db.execute("CREATE TABLE chatroom_member(room_id INTEGER, member_id INTEGER)")
            db.execute("INSERT INTO contact VALUES (?,?,?,?)",
                       ("group_ai@chatroom", "All-In-AI", "", ""))
            db.execute("INSERT INTO chat_room VALUES (1,'group_ai@chatroom')")
            db.executemany("INSERT INTO chatroom_member VALUES (1,?)", [(n,) for n in range(276)])
        self.assertFalse(Resolver(self.root).resolve("All-In-All").confirmed)
        self.assertFalse(Resolver(self.root).resolve("All-In-All", member_count=275).confirmed)
        self.assertEqual(Resolver(self.root).resolve("All-In-All", member_count=276).username,
                         "group_ai@chatroom")

        with closing(sqlite3.connect(self.root / "contact" / "contact.db")) as db, db:
            db.execute("INSERT INTO contact VALUES (?,?,?,?)",
                       ("group_all@chatroom", "All-In-All", "", ""))
            db.execute("INSERT INTO chat_room VALUES (2,'group_all@chatroom')")
            db.executemany("INSERT INTO chatroom_member VALUES (2,?)", [(n,) for n in range(276)])
        self.assertFalse(Resolver(self.root).resolve("All-In-All", member_count=276).confirmed)

    def test_truncated_group_title_requires_unique_matching_member_count(self):
        title = "【绿地都市之门】美团拼好饭群"
        with closing(sqlite3.connect(self.root / "contact" / "contact.db")) as db, db:
            db.execute("CREATE TABLE chat_room(id INTEGER, username TEXT)")
            db.execute("CREATE TABLE chatroom_member(room_id INTEGER, member_id INTEGER)")
            db.execute("INSERT INTO contact VALUES (?,?,?,?)",
                       ("group_food@chatroom", title, "", ""))
            db.execute("INSERT INTO chat_room VALUES (1,'group_food@chatroom')")
            db.executemany("INSERT INTO chatroom_member VALUES (1,?)", [(n,) for n in range(122)])
        visible = "【绿地都市之门】美团拼好."
        self.assertFalse(Resolver(self.root).resolve(visible, member_count=122).confirmed)
        self.assertFalse(Resolver(self.root).resolve(visible, member_count=121, truncated=True).confirmed)
        self.assertEqual(Resolver(self.root).resolve(visible, member_count=122,
                                                     truncated=True).username, "group_food@chatroom")

        with closing(sqlite3.connect(self.root / "contact" / "contact.db")) as db, db:
            db.execute("INSERT INTO contact VALUES (?,?,?,?)",
                       ("group_other@chatroom", "【绿地都市之门】美团拼好活动群", "", ""))
            db.execute("INSERT INTO chat_room VALUES (2,'group_other@chatroom')")
            db.executemany("INSERT INTO chatroom_member VALUES (2,?)", [(n,) for n in range(122)])
        self.assertFalse(Resolver(self.root).resolve(visible, member_count=122,
                                                 truncated=True).confirmed)

    def test_ocr_removed_space_in_group_name_requires_unique_matching_count(self):
        with closing(sqlite3.connect(self.root / "contact" / "contact.db")) as db, db:
            db.execute("CREATE TABLE chat_room(id INTEGER, username TEXT)")
            db.execute("CREATE TABLE chatroom_member(room_id INTEGER, member_id INTEGER)")
            db.execute("INSERT INTO contact VALUES (?,?,?,?)",
                       ("kimi@chatroom", "Kimi 大模型交流群", "", ""))
            db.execute("INSERT INTO chat_room VALUES (1,'kimi@chatroom')")
            db.executemany("INSERT INTO chatroom_member VALUES (1,?)", [(n,) for n in range(205)])
        resolver = Resolver(self.root)
        self.assertEqual(resolver.resolve("Kimi大模型交流群", member_count=205).username,
                         "kimi@chatroom")
        self.assertFalse(resolver.resolve("Kimi大模型交流群", member_count=204).confirmed)
        with closing(sqlite3.connect(self.root / "contact" / "contact.db")) as db, db:
            db.execute("INSERT INTO contact VALUES (?,?,?,?)",
                       ("other@chatroom", "Kimi大模型交流群", "", ""))
            db.execute("INSERT INTO chat_room VALUES (2,'other@chatroom')")
            db.executemany("INSERT INTO chatroom_member VALUES (2,?)", [(n,) for n in range(205)])
        self.assertFalse(Resolver(self.root).resolve("Kimi大模型交流群",
                                                     member_count=205).confirmed)

    def test_duplicate_needs_distinct_visible_messages(self):
        self.messages("wxid_a", "今晚七点到楼下", "记得带上门禁卡")
        self.messages("wxid_b", "你好", "周末见")
        resolver = Resolver(self.root)
        self.assertFalse(resolver.resolve("张三", ["今晚七点到楼下"]).confirmed)
        match = resolver.resolve("张三", ["今晚七点到楼下", "记得带上门禁卡"])
        self.assertEqual(match.username, "wxid_a")

    def test_plugin_policy_only_upscales_duplicate_titles(self):
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        recognition = plugin.Recognition(self.root)
        with patch.object(plugin, "read_identity_evidence", return_value=([], [])) as evidence:
            self.assertTrue(recognition.resolve("李四", [], [], image).confirmed)
            evidence.assert_not_called()
            self.assertFalse(recognition.resolve("张三", [], [], image).confirmed)
            evidence.assert_called_once()
            self.assertFalse(recognition.resolve("张三", [], [], image).confirmed)
            evidence.assert_called_once()

    def test_session_baseline_probe_runs_once_per_copy_version(self):
        recognition = plugin.Recognition(self.root)
        session = self.root / "session"
        session.mkdir()
        path = session / "session.db"
        path.write_bytes(b"invalid")
        self.assertTrue(recognition.needs_refresh())
        self.assertFalse(recognition.needs_refresh())
        path.write_bytes(b"updated copy")
        self.assertTrue(recognition.needs_refresh())

    def test_shared_messages_do_not_confirm(self):
        self.messages("wxid_a", "今晚七点到楼下", "记得带上门禁卡")
        self.messages("wxid_b", "今晚七点到楼下", "明天再说吧")
        match = Resolver(self.root).resolve("张三", ["今晚七点到楼下", "记得带上门禁卡"])
        self.assertFalse(match.confirmed)

    def test_fresh_clear_unread_plus_distinct_message_disambiguates(self):
        self.messages("wxid_a", "今晚七点到楼下")
        self.messages("wxid_b", "周末见")
        resolver = Resolver(self.root)
        self.assertFalse(resolver.resolve("张三", ["今晚七点到楼下"]).confirmed)
        match = resolver.resolve("张三", ["今晚七点到楼下"], "wxid_a")
        self.assertEqual(match.username, "wxid_a")
        self.assertFalse(resolver.resolve("张三", ["今晚七点到楼下"], "wxid_b").confirmed)
        self.assertFalse(resolver.resolve("不存在", ["今晚七点到楼下"], "wxid_a").confirmed)

    def test_three_short_messages_need_order_and_screen_timestamp(self):
        first = int(datetime(2026, 5, 13, 23, 27, 49).timestamp())
        table = "Msg_" + hashlib.md5(b"wxid_a").hexdigest()
        with closing(sqlite3.connect(self.root / "message" / "message_0.db")) as db, db:
            db.execute(f'CREATE TABLE "{table}"(create_time INTEGER, message_content TEXT)')
            db.executemany(f'INSERT INTO "{table}" VALUES (?,?)',
                           [(first, "1"), (first + 1, "1"), (first + 150, "2")])
        resolver = Resolver(self.root)
        self.assertFalse(resolver.resolve("张三", ["1", "1", "2"]).confirmed)
        self.assertFalse(resolver.resolve("张三", ["1", "2", "1"], time_labels=["5月13日23:27"]).confirmed)
        self.assertFalse(resolver.resolve("张三", ["1", "1", "2"], time_labels=["5月14日23:27"]).confirmed)
        self.assertEqual(resolver.resolve("张三", ["1", "1", "2"],
                                          time_labels=["5月13日23:27"]).username, "wxid_a")
        other = "Msg_" + hashlib.md5(b"wxid_b").hexdigest()
        with closing(sqlite3.connect(self.root / "message" / "message_0.db")) as db, db:
            db.execute(f'CREATE TABLE "{other}"(create_time INTEGER, message_content TEXT)')
            db.executemany(f'INSERT INTO "{other}" VALUES (?,?)',
                           [(first, "1"), (first + 1, "1"), (first + 150, "2")])
        self.assertFalse(resolver.resolve("张三", ["1", "1", "2"],
                                          time_labels=["5月13日23:27"]).confirmed)

    def test_uia_short_sequence_confirms_before_message_ocr(self):
        with closing(sqlite3.connect(self.root / "contact" / "contact.db")) as db, db:
            db.execute("INSERT INTO contact VALUES (?,?,?,?)", ("a_ne@wx", "嗯呢", "", ""))
            db.execute("INSERT INTO contact VALUES (?,?,?,?)", ("b_ne@wx", "嗯呢", "", ""))
        table = "Msg_" + hashlib.md5(b"a_ne@wx").hexdigest()
        start = int(datetime(2026, 5, 13, 23, 27).timestamp())
        with closing(sqlite3.connect(self.root / "message" / "message_0.db")) as db, db:
            db.execute(f'CREATE TABLE "{table}"(create_time INTEGER, message_content TEXT)')
            db.executemany(f'INSERT INTO "{table}" VALUES (?,?)',
                           [(start, "1"), (start + 1, "1"), (start + 180, "2")])
        match = plugin.Recognition(self.root).resolve_fast(
            "嗯呢", ("1", "1", "2"), ("5月13日 23:27",), switched=True)
        self.assertEqual(match.username, "a_ne@wx")
        recognition = plugin.Recognition(self.root)
        with patch.object(recognition.session_signal, "observe", return_value="b_ne@wx"):
            self.assertFalse(recognition.resolve_fast(
                "嗯呢", ("1", "1", "2"), ("5月13日 23:27",)).confirmed)

    def test_session_signal_requires_new_unique_clear_near_switch(self):
        session = self.root / "session"
        session.mkdir()
        path = session / "session.db"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE SessionTable(username TEXT, last_clear_unread_timestamp INTEGER)")
            db.executemany("INSERT INTO SessionTable VALUES (?,?)", [("wxid_a", 100), ("wxid_b", 50)])
        signal = SessionSignal(self.root)
        self.assertEqual(signal.observe(switched=True, now=10, wall=100), "")
        self.assertEqual(signal.observe(now=11, wall=100), "")
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE SessionTable SET last_clear_unread_timestamp=101 WHERE username='wxid_a'")
        self.assertEqual(signal.observe(now=12, wall=101), "wxid_a")
        self.assertEqual(signal.observe(now=15, wall=101), "")
        self.assertEqual(signal.observe(switched=True, now=20, wall=102), "")
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE SessionTable SET last_clear_unread_timestamp=102 WHERE username='wxid_a'")
            db.execute("UPDATE SessionTable SET last_clear_unread_timestamp=102 WHERE username='wxid_b'")
        self.assertEqual(signal.observe(now=21, wall=102), "")

    def test_new_session_copy_first_observation_is_only_baseline(self):
        signal = SessionSignal(self.root)
        self.assertEqual(signal.observe(switched=True, now=10, wall=100), "")
        session = self.root / "session"
        session.mkdir()
        path = session / "session.db"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE SessionTable(username TEXT, last_clear_unread_timestamp INTEGER)")
            db.execute("INSERT INTO SessionTable VALUES ('wxid_a', 100)")
        self.assertEqual(signal.observe(now=11, wall=100), "")
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE SessionTable SET last_clear_unread_timestamp=101")
        self.assertEqual(signal.observe(now=12, wall=101), "wxid_a")

    def test_session_signal_rejects_late_snapshot_of_old_clear(self):
        session = self.root / "session"
        session.mkdir()
        path = session / "session.db"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE SessionTable(username TEXT, last_clear_unread_timestamp INTEGER)")
            db.execute("INSERT INTO SessionTable VALUES ('wxid_a', 100)")
        signal = SessionSignal(self.root)
        signal.observe(switched=True, now=10, wall=1000)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE SessionTable SET last_clear_unread_timestamp=200")
        self.assertEqual(signal.observe(now=11, wall=1001), "")

    def test_session_signal_rejects_clear_before_this_switch(self):
        session = self.root / "session"
        session.mkdir()
        path = session / "session.db"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE SessionTable(username TEXT, last_clear_unread_timestamp INTEGER)")
            db.execute("INSERT INTO SessionTable VALUES ('wxid_a', 100)")
        signal = SessionSignal(self.root)
        signal.observe(switched=True, now=10, wall=200)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE SessionTable SET last_clear_unread_timestamp=195")
        self.assertEqual(signal.observe(now=11, wall=201), "")

    def test_compressed_messages_can_disambiguate(self):
        import zstandard
        pack = zstandard.ZstdCompressor().compress
        self.messages("wxid_a", pack("今晚七点到楼下".encode()), pack("记得带上门禁卡".encode()))
        self.messages("wxid_b", "周末见")
        match = Resolver(self.root).resolve("张三", ["今晚七点到楼下", "记得带上门禁卡"])
        self.assertEqual(match.username, "wxid_a")

    def test_missing_snapshot_stays_unconfirmed(self):
        self.assertFalse(Resolver().resolve("李四").confirmed)
        (self.root / "contact" / "contact.db").unlink()
        self.assertFalse(Resolver(self.root).resolve("李四").confirmed)


if __name__ == "__main__":
    unittest.main()
