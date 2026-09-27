import json
import os
from pathlib import Path
import queue
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

import main
from app.overlay import Overlay
from app.plugin_loader import load_plugin


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin("contact_profile", "profile_provider")
        self.record = {"memoryType": "ahu_crm_profile", "time": "2", "content": {
            "scopeKey": "current", "currentGrade": "B类客户",
            "communicationSummary": "关注课程安排和通知书",
            "studyBasics": "近期学习单词",
            "followupSuggestions": [{"action": "跟进课程安排", "kind": "AI建议"}],
            "recentEmotion": {"items": [{"emotionText": "学习辛苦"}]},
            "sopStages": [{"stage": "探需", "status": "未识别"}],
            "fieldEvidence": [{"quote": "通知书何时寄出"}],
        }}

    def test_only_crm_profile_is_rendered(self):
        event = {"memoryType": "crm_key_event", "content": {"originalMessages": "private chat"}}
        result = self.plugin.parse_response({"code": 0, "data": {"items": [event, self.record]}})
        self.assertTrue(result["has_profile"])
        self.assertEqual(result["grade"], "B类客户")
        self.assertEqual(result["suggestions"][0]["action"], "跟进课程安排")
        self.assertEqual(result["memories"], [])
        self.assertNotIn("private chat", repr(result))

    def test_key_event_goes_to_memory_tab_without_original_messages(self):
        event = {"memoryType": "crm_key_event", "time": 1_700_000_000_000,
                 "content": {"eventType": "course_interest", "actorRole": "customer",
                             "payloadJson": {"statedValue": "关注课程时间"},
                             "evidenceJson": [{"quote": "想了解周末课程"}],
                             "originalMessages": "private chat"}}
        result = self.plugin.parse_response({"code": 0, "data": {"items": [event]}})
        self.assertFalse(result["has_profile"])
        self.assertEqual(result["memories"][0]["detail"], "关注课程时间")
        self.assertEqual(result["memories"][0]["evidence"], ["想了解周末课程"])
        self.assertNotIn("private chat", repr(result))

    def test_fetch_uses_fixed_search_arguments_without_shell(self):
        seen = []

        def runner(args, **kwargs):
            seen.append((args, kwargs))
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "code": 0, "data": {"items": [self.record]}}))

        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "profile.json"
            config.write_text(json.dumps({"customer_ref": "v1.test-" + "a" * 32}), encoding="utf-8")
            result = self.plugin.fetch_profile(config, runner=runner, cli="ahucli.cmd")
        self.assertEqual(result["summary"], "关注课程安排和通知书")
        self.assertEqual(result["query"], "客户关注哪些课程？")
        self.assertEqual(seen[0][0][1:3], ["memory", "search"])
        self.assertIn("--customer-ref", seen[0][0])
        self.assertEqual(seen[0][0][-5:], ["--query", "客户关注哪些课程？", "--limit", "10", "--json"])
        self.assertNotIn("shell", seen[0][1])
        self.assertEqual(seen[0][1]["timeout"], 20)

    def test_custom_query_is_forwarded_as_one_argument(self):
        seen = []

        def runner(args, **kwargs):
            seen.append(args)
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "code": 0, "data": {"items": [self.record]}}))

        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "profile.json"
            config.write_text(json.dumps({"customer_ref": "v1.test-" + "a" * 32}), encoding="utf-8")
            result = self.plugin.fetch_profile(config, runner=runner, cli="ahucli.cmd",
                                               query="  最近有哪些关键事件？  ")
            with self.assertRaisesRegex(ValueError, "查询问题不能为空"):
                self.plugin.fetch_profile(config, runner=runner, cli="ahucli.cmd", query="   ")
        self.assertEqual(seen[0][seen[0].index("--query") + 1], "最近有哪些关键事件？")
        self.assertEqual(result["query"], "最近有哪些关键事件？")
        self.assertEqual(len(seen), 1)

    def test_empty_search_is_a_valid_result_and_invalid_config_fails_closed(self):
        empty = self.plugin.parse_response({"code": 0, "data": {"items": []}})
        self.assertFalse(empty["has_profile"])
        self.assertEqual(empty["memories"], [])
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "profile.json"
            config.write_text('{"customer_ref":"bad"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "参数无效"):
                self.plugin.fetch_profile(config, cli="ahucli.cmd")

    def test_refresh_runs_cli_in_background(self):
        events = queue.Queue()
        overlay = Mock()
        overlay.profile_query.return_value = "最近有哪些关键事件？"
        fake = SimpleNamespace(fetch_profile=lambda query: {"grade": "B类客户", "query": query})

        def immediate_thread(target, daemon):
            return Mock(start=lambda: target())

        with (patch.object(main, "ov", overlay, create=True),
              patch.object(main, "cli_profile_result", events),
              patch.object(main, "load_plugin", return_value=fake),
              patch.object(main.threading, "Thread", side_effect=immediate_thread)):
            main.refresh_cli_profile(show_results=True)
        overlay.set_external_profile.assert_called_once_with(loading=True)
        self.assertEqual(events.get_nowait(),
                         ({"grade": "B类客户", "query": "最近有哪些关键事件？"}, "", True))

    def test_empty_query_does_not_start_cli(self):
        overlay = Mock()
        overlay.profile_query.return_value = ""
        with (patch.object(main, "ov", overlay, create=True),
              patch.object(main, "load_plugin") as loader):
            main.refresh_cli_profile()
        overlay.set_external_profile.assert_called_once_with(error="请输入查询问题")
        loader.assert_not_called()

    def test_profile_ui_has_no_placeholder_name_and_compact_query(self):
        with patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}):
            calls = []
            overlay = Overlay(lambda *_: None,
                              on_refresh_profile=lambda manual: calls.append((manual, overlay.profile_query())))
            try:
                overlay.set_external_profile({"has_profile": True, "name": "", "grade": "B类客户"})
                overlay.app.processEvents()
                self.assertTrue(overlay.profileName.isHidden())
                self.assertEqual(overlay.profileQueryBox.height(), 34)
                overlay.set_external_profile({"has_profile": True, "name": "真实姓名"})
                overlay.app.processEvents()
                self.assertFalse(overlay.profileName.isHidden())
                overlay.set_wechat_contact({"username": "wxid_a", "nickname": "微信昵称",
                                            "remark": "微信备注"})
                self.assertEqual(overlay.wechatNickname.text(), "微信昵称")
                self.assertIn("微信备注", overlay.wechatRemark.text())
                overlay.app.processEvents()
                contact_bottom = overlay.wechatContact.mapTo(
                    overlay.profilePanel, overlay.wechatContact.rect().bottomLeft()).y()
                self.assertLess(contact_bottom, overlay.profileTabs.y())
                overlay.profileTabs.setCurrentIndex(1)
                overlay.app.processEvents()
                self.assertTrue(overlay.wechatContact.isVisible())
                self.assertEqual(overlay.wechatNickname.text(), "微信昵称")
                overlay.set_wechat_contact()
                self.assertTrue(overlay.wechatRemark.isHidden())
                self.assertEqual(overlay.wechatNickname.text(), "当前微信会话未确认")
                overlay.profileQueryBox.setFocus()
                overlay.profileQueryBox.selectAll()
                QTest.keyClicks(overlay.profileQueryBox, "CUSTOM QUERY 123")
                QTest.keyClick(overlay.profileQueryBox, Qt.Key_Return)
                self.assertEqual(calls[-1], (True, "CUSTOM QUERY 123"))
                overlay.profileQueryBox.setText("SECOND QUERY")
                overlay.profileRefreshButton.click()
                self.assertEqual(calls[-1], (True, "SECOND QUERY"))
                overlay.set_external_profile({"has_profile": False, "query": "CUSTOM QUERY 123",
                                              "memories": [{"title": "事件", "detail": "命中内容"}]},
                                             show_results=True)
                self.assertEqual(overlay.profileViews.currentIndex(), 1)
                self.assertIn("CUSTOM QUERY 123", overlay.memoryQuery.text())
                self.assertIn("1 条记忆", overlay.profileStatus.text())
            finally:
                overlay.win.close()


if __name__ == "__main__":
    unittest.main()
