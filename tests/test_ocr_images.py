import unittest
from unittest.mock import patch

import numpy as np

from app import ocr


def box(left, top, right, bottom):
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


class ImageOcrTests(unittest.TestCase):
    def test_large_image_text_is_one_message_and_avatar_text_is_ignored(self):
        detections = [
            (box(5, 10, 38, 30), "头像字", 0.9),
            (box(145, 100, 285, 120), "最容易掉进一个陷阱", 0.9),
            (box(145, 140, 385, 160), "新工具的勤奋，逃避运营项目", 0.9),
            (box(145, 180, 520, 200), "真正决定结果的是后续反馈", 0.9),
        ]

        class FakeEngine:
            def __call__(self, image, use_cls=False):
                return detections, None

        with (patch.object(ocr, "_engine", return_value=FakeEngine()),
              patch.object(ocr, "who_said", return_value=(None, np.array([255, 255, 255]), 0))):
            reader = ocr.Reader()
            lines = reader.read(np.zeros((250, 600, 3), dtype=np.uint8), np.zeros(3))
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0][0], "image")
        self.assertIn("最容易掉进一个陷阱", lines[0][2])
        self.assertIn("真正决定结果的是后续反馈", lines[0][2])
        self.assertNotIn("头像字", lines[0][2])


if __name__ == "__main__":
    unittest.main()
