from contextlib import closing
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from app.history import read_page
from app.overlay import Overlay
from app.plugin_loader import load_plugin
import main


class HistoryTests(unittest.TestCase):
    def test_non_text_messages_never_show_raw_xml(self):
        with tempfile.TemporaryDirectory() as temp:
            snapshot = Path(temp)
            (snapshot / "message").mkdir()
            username = "wxid_one"
            table = "Msg_" + hashlib.md5(username.encode()).hexdigest()
            with closing(sqlite3.connect(snapshot / "message" / "message_0.db")) as db, db:
                db.execute(f'CREATE TABLE "{table}"(create_time INTEGER, message_content TEXT, local_type INTEGER)')
                db.executemany(f'INSERT INTO "{table}" VALUES (?,?,?)', [
                    (1, "你好", 1),
                    (2, '<voipmsg type="VoIPBubbleMsg"><VoIPBubbleMsg><msg><![CDATA[已在其它设备接听]]>'
                     '</msg></VoIPBubbleMsg></voipmsg>', 50),
                    (3, '<msg><videomsg/></msg>', 43),
                ])
            rows, _, _ = read_page(snapshot, username)
            self.assertEqual([row[4] for row in rows], ["你好", "[通话] 已在其它设备接听", "[视频]"])
            self.assertNotIn("<voipmsg", repr(rows))

    def test_database_latest_uses_incoming_row_not_ocr(self):
        rows = [
            (1, "message_2.db", 1, "wxid_other", "旧消息", ""),
            (2, "message_2.db", 2, "wxid_self", "快发啊", ""),
            (3, "message_2.db", 3, "wxid_other", "发啥", ""),
        ]
        self.assertEqual(main.latest_incoming_from_rows(rows, "wxid_other", "C:/wxid_self/db_storage"), "发啥")
        with patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}):
            overlay = Overlay(lambda *_: None)
            try:
                overlay.set_chat("chat")
                overlay.log_message("her", "白", chat="chat")
                overlay.set_database_latest("chat", "发啥")
                self.assertEqual(overlay.latest.text(), "发啥")
            finally:
                overlay.win.close()

    def test_image_message_uses_matching_local_cache_and_not_xml(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            account = root / "account_1234"
            (account / "db_storage").mkdir(parents=True)
            cache = root / "cache"
            snapshot = cache / "snapshot-test"
            (snapshot / "message").mkdir(parents=True)
            (cache / "ready.json").write_text(json.dumps({
                "generation": snapshot.name, "source": str(account / "db_storage")}), encoding="utf-8")
            username = "wxid_one"
            media_hash = "a" * 32
            attachment = account / "msg" / "attach" / hashlib.md5(username.encode()).hexdigest() / "2026-09" / "Img"
            attachment.mkdir(parents=True)
            picture = BytesIO()
            Image.new("RGB", (12, 8), "red").save(picture, format="PNG")
            (attachment / (media_hash + ".dat")).write_bytes(bytes(value ^ 83 for value in picture.getvalue()))
            table = "Msg_" + hashlib.md5(username.encode()).hexdigest()
            xml = '<?xml version="1.0"?><msg><img aeskey="secret"/></msg>'
            with closing(sqlite3.connect(snapshot / "message" / "message_0.db")) as db, db:
                db.execute(f'CREATE TABLE "{table}"(create_time INTEGER, message_content TEXT, '
                           'local_type INTEGER, packed_info_data BLOB)')
                db.execute(f'INSERT INTO "{table}" VALUES (100, ?, 3, ?)',
                           (xml, b"\x08\x04\x22\x20" + media_hash.encode() + b"\x58\x00"))
            rows, _, _ = read_page(snapshot, username)
            self.assertEqual(rows[0][4], "[图片，点击查看大图]")
            self.assertTrue(Path(rows[0][5]).is_file())
            self.assertNotIn("<img", repr(rows))
            plugin = load_plugin("chat_media", "media_preview")
            label, path = plugin.preview_message(snapshot, "wxid_other", 3, xml, media_hash.encode())
            self.assertEqual(path, "")
            self.assertIn("未缓存", label)

    def test_image_without_local_index_has_clear_placeholder(self):
        plugin = load_plugin("chat_media", "media_preview")
        label, path = plugin.preview_message("unused", "wxid_one", 3, "<msg><img/></msg>", None)
        self.assertEqual((label, path), ("[图片：缺少本地附件索引]", ""))
        self.assertTrue(plugin.is_image_message("", "Alice:\n<?xml version='1.0'?><msg><img/></msg>"))

    def test_v2_image_cache_decodes_without_process_access(self):
        plugin = load_plugin("chat_media", "media_preview")
        key = b"0123456789abcdef"
        plain = b"head" + bytes([12]) * 12
        encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        cipher = encryptor.update(plain) + encryptor.finalize()
        data = b"\x07\x08V2\x08\x07" + struct.pack("<II", 4, 3) + b"\x00" + cipher + b"middle"
        data += bytes(value ^ 81 for value in b"end")
        self.assertEqual(plugin.decode_dat(data, key, 81), b"headmiddleend")

    def test_history_feed_renders_image_thumbnail(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}):
            path = Path(temp) / "preview.png"
            Image.new("RGB", (12, 8), "red").save(path)
            overlay = Overlay(lambda *_: None)
            try:
                overlay.set_chat("chat")
                overlay.set_history_page("chat", [(100, "message_0.db", 1, "Alice",
                                                  "[图片，点击查看大图]", str(path))],
                                         False, first_page=True)
                overlay.app.processEvents()
                self.assertIn("img", overlay.feed.toHtml())
                self.assertIn("href=", overlay.feed.toHtml())
                self.assertIn("Alice", overlay.feed.toPlainText())
            finally:
                overlay.win.close()

    def test_history_panel_contains_only_database_rows(self):
        with patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}):
            overlay = Overlay(lambda *_: None)
            try:
                overlay.set_chat("chat")
                overlay.set_history_page("chat", [(100, "message_0.db", 1, "Alice",
                                                  "database message", "")], False, first_page=True)
                overlay.log_message("her", "OCR GHOST", "Alice", chat="chat")
                overlay.log("capture warning")
                self.assertIn("database message", overlay.feed.toPlainText())
                self.assertNotIn("OCR GHOST", overlay.feed.toPlainText())
                self.assertNotIn("capture warning", overlay.feed.toPlainText())
                self.assertIn("· 1", overlay.historyButton.text())
                self.assertEqual(overlay.latest.text(), "OCR GHOST")
                overlay.set_history_page("chat", [], False, first_page=True)
                self.assertEqual(overlay.feed.toPlainText(), "")
                self.assertNotIn("· 1", overlay.historyButton.text())
            finally:
                overlay.win.close()

    def test_reopening_history_refreshes_cached_images(self):
        with patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}):
            requested = []
            overlay = Overlay(lambda *_: None, on_load_history=lambda *args, **kwargs:
                              requested.append((args, kwargs)))
            try:
                overlay.set_chat("chat")
                overlay._toggle_history()
                self.assertEqual(requested, [(('chat',), {'refresh': True})])
                overlay._toggle_history()
                overlay._toggle_history()
                self.assertEqual(len(requested), 2)
            finally:
                overlay.win.close()

    def test_refresh_discards_old_page_cursor(self):
        old = {"cursor": (50, "message_0.db", 1), "loaded": True,
               "loading": False, "more": False}
        with (patch.object(main, "history_sources", {"chat": ("snapshot", "wxid_one")}),
              patch.object(main, "history_state", {"chat": old}) as pages,
              patch.object(main, "ov", Mock(), create=True),
              patch.object(main.threading, "Thread") as thread):
            main.on_load_history("chat", refresh=True)
            self.assertIsNone(pages["chat"]["cursor"])
            self.assertFalse(pages["chat"]["loaded"])
            self.assertTrue(pages["chat"]["loading"])
            thread.return_value.start.assert_called_once()

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
