import unittest
import queue
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main


class FollowWindowTests(unittest.TestCase):
    def test_image_ocr_is_displayed_without_triggering_reply(self):
        incoming = Mock()
        incoming.get_nowait.side_effect = [
            ("lines", "chat", [("image", None, "[图片文字] 一段文字")], (1, 2, 3, 4)), queue.Empty]
        overlay = Mock()
        overlay.current_chat.return_value = "chat"
        state = {"area": None, "chat": "chat", "confirmed": True,
                 "busy": False, "rerun": None}
        with (patch.object(main, "q", incoming, create=True),
              patch.object(main, "ov", overlay, create=True),
              patch.object(main, "state", state),
              patch.object(main, "chats", {}),
              patch.object(main, "start_analyze") as generate):
            main.drain()
            self.assertEqual(list(main.chat_of("chat")["history"]), [])
        overlay.log_message.assert_called_once_with("image", "[图片文字] 一段文字", None, chat="chat")
        generate.assert_not_called()

    def test_manual_choice_requires_current_window_and_account(self):
        context = (11, "account", "snapshot", "张三", "row:abc")
        state = {"hwnd": 11, "account_root": "account", "snapshot": "snapshot",
                 "candidate_context": context}
        commands, overlay = Mock(), Mock()
        with (patch.object(main, "state", state), patch.object(main, "commands", commands),
              patch.object(main, "ov", overlay, create=True),
              patch.object(main, "capture_on", Mock(is_set=Mock(return_value=True)), create=True)):
            main.on_confirm_identity((22, *context[1:]), "wxid_a")
            commands.put.assert_not_called()
            main.on_confirm_identity(context, "wxid_a")
            commands.put.assert_called_once_with(("confirm", *context, "wxid_a"))

    def test_manual_fill_rechecks_selected_row(self):
        state = {"hwnd": 11, "chat": "张三 [id:wxid_a]", "confirmed": True,
                 "area": (1, 2, 3, 4), "manual_context": ("张三", "row:old"),
                 "account_root": "C:/account/db_storage"}
        overlay = Mock()
        overlay.current_chat.return_value = state["chat"]
        observation = SimpleNamespace(
            header=SimpleNamespace(title="张三"), visible=(), labels=(), selection_token="new")
        plugin = SimpleNamespace(read_window_context=lambda hwnd: observation)
        commands = Mock()
        with (patch.object(main, "state", state), patch.object(main, "ov", overlay, create=True),
              patch.object(main, "commands", commands),
              patch.object(main, "capture_on", Mock(is_set=Mock(return_value=True)), create=True),
              patch.object(main, "load_plugin", return_value=plugin),
              patch.object(main, "account_for_window", return_value=Path("C:/account/db_storage")),
              patch.object(main, "fill") as fill):
            with self.assertRaisesRegex(RuntimeError, "重新确认身份"):
                main.fill_reply("hello")
        self.assertFalse(state["confirmed"])
        commands.put.assert_called_once_with(("revoke", 11))
        fill.assert_not_called()

    def test_legacy_snapshot_refreshes_only_session_copy(self):
        with tempfile.TemporaryDirectory() as snapshot:
            state = {"account_root": "account", "snapshot": snapshot,
                     "hwnd": 11, "chat": "", "confirmed": False, "next_db_sync": 0}
            lock = Mock()
            lock.acquire.return_value = True
            events = queue.Queue()

            def immediate_thread(target, daemon):
                return Mock(start=lambda: target())

            with (patch.object(main, "state", state),
                  patch.object(main, "capture_on", Mock(is_set=Mock(return_value=True)), create=True),
                  patch.object(main, "sync_lock", lock),
                  patch.object(main, "sync_result", events),
                  patch.object(main.threading, "Thread", side_effect=immediate_thread),
                  patch("app.db_incremental.refresh_legacy_session", return_value=True) as refresh,
                  patch("app.db_incremental.refresh_legacy_contact", return_value=False) as contact_refresh):
                main.poll_database_sync()
            lock.acquire.assert_called_once_with(blocking=False)
            refresh.assert_called_once()
            contact_refresh.assert_called_once()
            self.assertEqual(events.get_nowait()[2], ["session/session.db"])

    def test_first_visible_messages_are_context_not_new_triggers(self):
        incoming = Mock()
        incoming.get_nowait.side_effect = [
            ("baseline", "chat", [("her", "Alice", "旧消息")], (1, 2, 3, 4)), queue.Empty]
        overlay = Mock()
        overlay.current_chat.return_value = "chat"
        state = {"area": None, "chat": "chat", "confirmed": True,
                 "busy": False, "rerun": None}
        with (patch.object(main, "q", incoming, create=True),
              patch.object(main, "ov", overlay, create=True),
              patch.object(main, "state", state),
              patch.object(main, "chats", {}),
              patch.object(main, "start_analyze") as generate):
            main.drain()
            self.assertEqual(list(main.chat_of("chat")["history"]), [("her", "旧消息", "Alice")])
        generate.assert_not_called()

    def test_confirmed_chat_clears_old_identity_warning(self):
        incoming = Mock()
        incoming.get_nowait.side_effect = [
            ("chat", "已确认会话", True, "", "snapshot", "wxid_a"), queue.Empty]
        overlay = Mock()
        state = {"chat": "旧会话", "confirmed": False}
        with (patch.object(main, "q", incoming, create=True),
              patch.object(main, "ov", overlay, create=True),
              patch.object(main, "state", state),
              patch.object(main, "history_sources", {}),
              patch.object(main, "history_state", {}),
              patch.object(main, "on_load_history")):
            main.drain()
        self.assertTrue(state["confirmed"])
        overlay.set_status.assert_called_with("会话已确认，等待对方的新消息", "idle")

    def test_switch_discards_old_capture_identity_and_queue(self):
        old_child, old_queue, new_child, new_queue = (Mock() for _ in range(4))
        overlay = Mock()
        state = {"hwnd": 11, "area": (1, 2, 3, 4), "chat": "old", "confirmed": True,
                 "rerun": ("old", []), "busy": True}
        chats = {"old": {"rev": 3}}
        with (patch.object(main, "capture_on", Mock(is_set=Mock(return_value=True)), create=True),
              patch.object(main, "find_wechat_hwnd", return_value=22),
              patch.object(main, "spawn_worker", return_value=new_child),
              patch.object(main.multiprocessing, "Queue", return_value=new_queue),
              patch.object(main, "child", old_child, create=True), patch.object(main, "q", old_queue, create=True),
              patch.object(main, "ov", overlay, create=True), patch.object(main, "state", state),
              patch.object(main, "chats", chats)):
            main.follow_wechat_window()
            self.assertIs(main.child, new_child)
            self.assertIs(main.q, new_queue)
        old_child.terminate.assert_called_once()
        old_child.join.assert_called_once()
        old_queue.close.assert_called_once()
        self.assertEqual(state["hwnd"], 22)
        self.assertFalse(state["confirmed"])
        self.assertIsNone(state["area"])
        self.assertEqual(chats["old"]["rev"], 4)
        overlay.invalidate_replies.assert_called_once()


if __name__ == "__main__":
    unittest.main()
