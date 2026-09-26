import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch
import sys

import numpy as np

from app.plugin_loader import load_plugin

plugin = load_plugin("session_recognition", "recognizer")
_choose_title_boxes, read_title = plugin.choose_title_boxes, plugin.read_title


def box(x, y, width, height, text):
    polygon = [[x, y], [x + width, y], [x + width, y + height], [x, y + height]]
    return polygon, text, 0.99


class TitleOcrTests(unittest.TestCase):
    def test_uia_context_reads_short_bubbles_and_date_from_current_window(self):
        class Control:
            def __init__(self, aid="", name="", children=(), selected=False):
                self.AutomationId, self.Name = aid, name
                self.children, self.selected = children, selected

            def GetChildren(self):
                return self.children

            def GetSelectionItemPattern(self):
                return SimpleNamespace(IsSelected=self.selected)

        title = Control("content_view.current_chat_name_label", "嗯呢")
        selected = Control("session_item_嗯呢", selected=True)
        sessions = Control("session_list", children=[selected])
        messages = Control("chat_message_list", children=[
            Control(name="5月13日 23:27"),
            *[Control("chat_message_list.qt_scrollarea_viewport.chat_bubble_item_view", value)
              for value in ("1", "1", "2")]])
        root = Control(children=[title, sessions, messages])
        fake = SimpleNamespace(UIAutomationInitializerInThread=nullcontext,
                               ControlFromHandle=lambda hwnd: root)
        with patch.dict(sys.modules, {"uiautomation": fake}):
            context = plugin.read_window_context(123)
        self.assertEqual(context.header.title, "嗯呢")
        self.assertEqual(context.visible, ("1", "1", "2"))
        self.assertEqual(context.labels, ("5月13日 23:27",))

    def test_uia_full_title_and_selected_row(self):
        base = "content_view.top_content_view.title_h_view.left_v_view.left_content_v_view.left_ui_.big_title_line_h_view."
        controls = [(base + "current_chat_name_label", "【绿地都市之门】美团拼好饭群", False),
                    (base + "current_chat_count_label", "(122)", False),
                    ("session_item_【绿地都市之门】美团拼好饭群", "", True)]
        header = plugin.choose_uia_header(controls)
        self.assertEqual(header.title, "【绿地都市之门】美团拼好饭群")
        self.assertEqual(header.member_count, 122)
        self.assertFalse(header.truncated)
        self.assertFalse(plugin.choose_uia_header(controls[:-1] +
                         [("session_item_另一个群", "", True)]).title)

    def test_uia_never_uses_selected_row_without_header(self):
        self.assertFalse(plugin.choose_uia_header(
            [("session_item_张三", "", True)]).title)

    def test_joins_split_title_and_removes_member_count(self):
        results = [box(12, 9, 68, 19, "山禾FDE"), box(83, 10, 80, 19, "交流群(343)"),
                   box(12, 57, 200, 15, "群公告（共2条）")]
        self.assertEqual(_choose_title_boxes(results, 420, 90), "山禾FDE交流群")
        self.assertEqual(plugin.choose_header_boxes(results, 420, 90).member_count, 343)

    def test_truncated_group_count_survives_icon_ocr_noise(self):
        header = plugin.choose_header_boxes(
            [box(12, 9, 270, 19, "【绿地都市之门】美团拼好.（122）Q")], 420, 80)
        self.assertEqual(header.title, "【绿地都市之门】美团拼好.")
        self.assertEqual(header.member_count, 122)
        self.assertTrue(header.truncated)

    def test_ocr_strips_space_between_latin_and_chinese_group_name(self):
        header = plugin.choose_header_boxes(
            [box(12, 9, 240, 19, "Kimi 大模型交流群(205)")], 420, 80)
        self.assertEqual(header.title, "Kimi大模型交流群")
        self.assertEqual(header.member_count, 205)

    def test_ignores_enterprise_badge_and_right_button(self):
        results = [box(12, 8, 38, 20, "张三"), box(54, 10, 70, 16, "@公司"),
                   box(380, 14, 18, 13, "***")]
        self.assertEqual(_choose_title_boxes(results, 420, 80), "张三")

    def test_does_not_use_announcement_as_title(self):
        self.assertEqual(_choose_title_boxes([box(12, 56, 180, 16, "群公告")], 420, 90), "")

    def test_ambiguous_title_band_stays_unknown(self):
        results = [box(12, 7, 60, 16, "张三"), box(12, 34, 60, 16, "李四")]
        self.assertEqual(_choose_title_boxes(results, 420, 100), "")

    def test_read_title_uses_box_selector(self):
        header = np.zeros((80, 420, 3), dtype=np.uint8)
        with patch.object(plugin, "ocr_boxes", return_value=[box(12, 8, 50, 18, "李四")]):
            self.assertEqual(read_title(header), "李四")


if __name__ == "__main__":
    unittest.main()
