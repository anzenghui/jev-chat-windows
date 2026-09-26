import threading
import queue
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from app import worker
from plugins.session_recognition.plugin import Match


class WorkerFastTests(unittest.TestCase):
    def test_manual_duplicate_choice_is_revoked_on_selected_row_change(self):
        events, commands = [], queue.Queue()
        clock = [0.0]

        class Sink:
            def put(self, item):
                events.append(item)
                if item[0] == "candidates" and item[6] and commands.empty():
                    commands.put(("confirm", item[1], item[3], item[4], item[2], item[5], "wxid_a"))

        class Capture:
            def __init__(self, hwnd):
                self.calls = 0

            def alive(self):
                self.calls += 1
                return self.calls <= 3

            def settled(self):
                return None

            def wait(self):
                pass

        class AccountProbe:
            revision = 1

            def __init__(self, hwnd):
                pass

            def poll(self):
                return Path("account/db_storage")

        class Recognition:
            def __init__(self, snapshot=""):
                pass

            def needs_refresh(self):
                return False

            def resolve_fast(self, title, *args, **kwargs):
                return Match(title, reason="同名", ambiguous=True)

            def candidate_options(self, *args):
                return [("wxid_a", "张三"), ("wxid_b", "张三")]

        reads = [0]

        def read_context(hwnd):
            reads[0] += 1
            return SimpleNamespace(
                header=SimpleNamespace(title="张三", member_count=None, truncated=False),
                visible=(), labels=(), selection_token="row-a" if reads[0] < 3 else "row-b")

        def sleep(seconds):
            clock[0] += 0.21

        plugin = SimpleNamespace(Recognition=Recognition, Match=Match, read_window_context=read_context)
        enabled = threading.Event()
        enabled.set()
        with (patch.object(worker, "load_plugin", return_value=plugin),
              patch.object(worker, "Capture", Capture),
              patch.object(worker, "AccountProbe", AccountProbe),
              patch.object(worker, "unminimize"),
              patch.object(worker, "snapshot_for_account", return_value=Path("snapshot")),
              patch.object(worker.time, "monotonic", side_effect=lambda: clock[0]),
              patch.object(worker.time, "sleep", side_effect=sleep)):
            worker.run(Sink(), 123, enabled, threading.Event(), commands)
        chats = [item for item in events if item[0] == "chat"]
        self.assertTrue(any(item[2] and item[5] == "wxid_a" for item in chats))
        self.assertFalse(chats[-1][2])
        self.assertIn("未确认", chats[-1][1])

    def test_uia_confirmation_does_not_wait_for_a_settled_frame(self):
        events = []

        class Capture:
            def __init__(self, hwnd):
                self.calls = 0

            def alive(self):
                self.calls += 1
                return self.calls == 1

            def settled(self):
                return None

            def wait(self):
                pass

        class Recognition:
            def __init__(self, snapshot=""):
                pass

            def needs_refresh(self):
                return False

            def resolve_fast(self, *args, **kwargs):
                return SimpleNamespace(confirmed=True, username="wxid_a", reason="")

        class AccountProbe:
            revision = 1

            def __init__(self, hwnd):
                pass

            def poll(self):
                return Path("account/db_storage")

        context = SimpleNamespace(
            header=SimpleNamespace(title="嗯呢", member_count=None, truncated=False),
            visible=("1", "1", "2"), labels=("5月13日 23:27",))
        plugin = SimpleNamespace(Recognition=Recognition, read_window_context=lambda hwnd: context)
        enabled = threading.Event()
        enabled.set()
        with (patch.object(worker, "load_plugin", return_value=plugin),
              patch.object(worker, "Capture", Capture),
              patch.object(worker, "AccountProbe", AccountProbe),
              patch.object(worker, "unminimize"),
              patch.object(worker, "chat_area", side_effect=AssertionError("no frame should be parsed")),
              patch.object(worker, "snapshot_for_account", return_value=Path("snapshot")),
              patch.object(worker.time, "sleep")):
            worker.run(SimpleNamespace(put=events.append),
                       123, enabled, threading.Event())
        self.assertTrue(any(item[0] == "chat" and item[2] for item in events))
        self.assertFalse(any(item[0] == "ocr" for item in events))

    def test_confirmed_uia_identity_arrives_before_message_ocr(self):
        events = []

        class Sink:
            def put(self, item):
                events.append(item)

        class Capture:
            def __init__(self, hwnd):
                self.calls = 0
                self.area = None

            def alive(self):
                self.calls += 1
                return self.calls == 1

            def settled(self):
                return np.zeros((100, 120, 3), dtype=np.uint8)

            def wait(self):
                pass

        class Reader:
            last_boxes = []

            def read(self, pixels, bg):
                events.append(("ocr",))
                return []

            def new_lines(self, lines):
                return []

        class Recognition:
            def __init__(self, snapshot=""):
                pass

            def needs_refresh(self):
                return False

            def resolve_fast(self, *args, **kwargs):
                return SimpleNamespace(confirmed=True, username="wxid_a", reason="")

        class AccountProbe:
            revision = 1

            def __init__(self, hwnd):
                pass

            def poll(self):
                return Path("account/db_storage")

        context = SimpleNamespace(
            header=SimpleNamespace(title="嗯呢", member_count=None, truncated=False),
            visible=("1", "1", "2"), labels=("5月13日 23:27",))
        plugin = SimpleNamespace(Recognition=Recognition, read_window_context=lambda hwnd: context)
        enabled = threading.Event()
        enabled.set()
        with (patch.object(worker, "load_plugin", return_value=plugin),
              patch.object(worker, "Capture", Capture),
              patch.object(worker, "AccountProbe", AccountProbe),
              patch.object(worker, "Reader", Reader),
              patch.object(worker, "unminimize"),
              patch.object(worker, "chat_area", return_value=(10, 20, 110, 90, np.zeros(3), 0)),
              patch.object(worker, "snapshot_for_account", return_value=Path("snapshot")),
              patch.object(worker.time, "sleep")):
            worker.run(Sink(), 123, enabled, threading.Event())
        confirmed = next(i for i, item in enumerate(events) if item[0] == "chat" and item[2])
        ocr = next(i for i, item in enumerate(events) if item[0] == "ocr")
        self.assertLess(confirmed, ocr)

    def test_account_change_invalidates_identity_before_new_confirmation(self):
        events = []

        class Capture:
            def __init__(self, hwnd):
                self.calls = 0

            def alive(self):
                self.calls += 1
                return self.calls <= 3

            def settled(self):
                return None

            def wait(self):
                pass

        class AccountProbe:
            def __init__(self, hwnd):
                self.revision = 0

            def poll(self):
                self.revision += 1
                return Path(f"account-{self.revision}/db_storage")

        class Recognition:
            def __init__(self, snapshot=""):
                self.snapshot = str(snapshot)

            def needs_refresh(self):
                return False

            def resolve_fast(self, *args, **kwargs):
                return SimpleNamespace(confirmed=True, username=self.snapshot, reason="")

        context = SimpleNamespace(
            header=SimpleNamespace(title="张三", member_count=None, truncated=False),
            visible=(), labels=())
        plugin = SimpleNamespace(Recognition=Recognition, read_window_context=lambda hwnd: context)
        enabled = threading.Event()
        enabled.set()
        with (patch.object(worker, "load_plugin", return_value=plugin),
              patch.object(worker, "Capture", Capture),
              patch.object(worker, "AccountProbe", AccountProbe),
              patch.object(worker, "unminimize"),
              patch.object(worker, "snapshot_for_account", side_effect=lambda root: Path(root.parent.name)),
              patch.object(worker.time, "sleep")):
            worker.run(SimpleNamespace(put=events.append), 123, enabled, threading.Event())
        chats = [item for item in events if item[0] == "chat"]
        self.assertEqual([item[2] for item in chats], [False, True] * 3)
        self.assertEqual([item[5] for item in chats if item[2]], ["account-1", "account-2", "account-3"])


if __name__ == "__main__":
    unittest.main()
