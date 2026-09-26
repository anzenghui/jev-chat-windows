"""A header-only change should reach the OCR worker without message movement."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from app.capture import Capture


class CaptureHeaderTests(unittest.TestCase):
    def test_header_change_creates_pending_frame(self):
        cap = Capture.__new__(Capture)
        cap.shape = cap.area = cap.last = cap.last_head = cap.pending = None
        cap.t = cap.t0 = 0.0
        area = (10, 10, 90, 50, np.array([1, 1, 1]), 0)
        frame = np.ones((60, 100, 4), dtype=np.uint8)
        with patch("app.capture.chat_area", return_value=area):
            cap.on_frame_arrived(SimpleNamespace(frame_buffer=frame), None)
            self.assertIsNotNone(cap.pending)
            cap.pending = None
            changed = frame.copy()
            changed[3, 30, :3] = 20
            cap.on_frame_arrived(SimpleNamespace(frame_buffer=changed), None)
            self.assertIsNotNone(cap.pending)
            cap.pending = None
            cap.on_frame_arrived(SimpleNamespace(frame_buffer=changed), None)
            self.assertIsNone(cap.pending)


if __name__ == "__main__":
    unittest.main()
