from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import accounts, capture


class AccountTests(unittest.TestCase):
    def test_account_probe_does_not_block_capture_and_reuses_result(self):
        released = threading.Event()
        root = Path("C:/account/db_storage")
        calls = []

        def lookup(hwnd):
            calls.append(hwnd)
            released.wait(2)
            return root

        probe = accounts.AccountProbe(11, interval=60, lookup=lookup)
        start = time.perf_counter()
        self.assertIsNone(probe.poll())
        self.assertLess(time.perf_counter() - start, 0.1)
        released.set()
        deadline = time.monotonic() + 2
        while not probe.ready and time.monotonic() < deadline:
            probe.poll()
            time.sleep(0.01)
        self.assertTrue(probe.ready)
        self.assertEqual(probe.root, root)
        revision = probe.revision
        self.assertEqual(probe.poll(), root)
        self.assertEqual(probe.revision, revision)
        self.assertEqual(calls, [11])

    def test_account_probe_ignores_result_started_before_reset(self):
        first_started = threading.Event()
        first_release = threading.Event()
        second_release = threading.Event()
        calls = []

        def lookup(_hwnd):
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                first_started.set()
                first_release.wait(2)
                return Path("C:/old/db_storage")
            second_release.wait(2)
            return Path("C:/new/db_storage")

        probe = accounts.AccountProbe(11, interval=60, lookup=lookup)
        probe.poll()
        self.assertTrue(first_started.wait(1))
        probe.reset()
        probe.poll()
        first_release.set()
        deadline = time.monotonic() + 2
        while len(calls) < 2 and time.monotonic() < deadline:
            probe.poll()
            time.sleep(0.01)
        self.assertEqual(len(calls), 2)
        self.assertFalse(probe.ready)
        second_release.set()
        while not probe.ready and time.monotonic() < deadline:
            probe.poll()
            time.sleep(0.01)
        self.assertEqual(probe.root, Path("C:/new/db_storage"))

    def test_window_selection_follows_foreground_and_never_guesses(self):
        class User32:
            def GetWindowThreadProcessId(self, hwnd, pid):
                pid._obj.value = {11: 101, 22: 202}[hwnd]
        with patch.object(capture, "u32", User32()):
            self.assertEqual(capture.choose_wechat_hwnd([11, 22], 22, 202, 11), 22)
            self.assertEqual(capture.choose_wechat_hwnd([11, 22], 33, 202, 11), 22)
            self.assertEqual(capture.choose_wechat_hwnd([11, 22], 33, 999, 11), 11)
            self.assertIsNone(capture.choose_wechat_hwnd([11, 22], 33, 999))

    def test_window_account_requires_one_open_database_root(self):
        with tempfile.TemporaryDirectory() as temp:
            roots = [Path(temp) / name / "db_storage" for name in ("a", "b")]
            for root in roots:
                (root / "contact").mkdir(parents=True)
                (root / "contact" / "contact.db").touch()
            process = SimpleNamespace(pid=7, name=lambda: "Weixin.exe", parent=lambda: None, children=lambda recursive: [],
                                      open_files=lambda: [SimpleNamespace(path=str(roots[0] / "contact" / "contact.db"))])
            with patch.object(accounts, "_window_pid", return_value=7), patch.object(accounts.psutil, "Process", return_value=process):
                self.assertEqual(accounts.account_for_window(11), roots[0].resolve())
                process.open_files = lambda: [SimpleNamespace(path=str(r / "contact" / "contact.db")) for r in roots]
                self.assertIsNone(accounts.account_for_window(11))

    def test_window_owned_database_skips_slow_descendant_scan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "a" / "db_storage"
            (root / "contact").mkdir(parents=True)
            (root / "contact" / "contact.db").touch()
            process = SimpleNamespace(pid=7, name=lambda: "Weixin.exe",
                                      open_files=lambda: [SimpleNamespace(path=str(root / "contact" / "contact.db"))],
                                      children=lambda recursive: self.fail("unique window-owned DB needs no descendants"))
            with patch.object(accounts, "_window_pid", return_value=7), patch.object(accounts.psutil, "Process", return_value=process):
                self.assertEqual(accounts.account_for_window(11), root.resolve())

    def test_snapshot_must_belong_to_selected_account(self):
        with tempfile.TemporaryDirectory() as temp:
            account = Path(temp) / "a" / "db_storage"
            wrong = Path(temp) / "b" / "db_storage"
            for root in (account, wrong):
                (root / "contact").mkdir(parents=True)
            with closing(sqlite3.connect(wrong / "contact" / "contact.db")) as db:
                db.execute("CREATE TABLE contact(username TEXT)")
                db.commit()
            self.assertIsNone(accounts.snapshot_for_account(account))
            with closing(sqlite3.connect(account / "contact" / "contact.db")) as db:
                db.execute("CREATE TABLE contact(username TEXT)")
                db.commit()
            self.assertIsNone(accounts.snapshot_for_account(account))

    def test_finds_only_selected_accounts_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp) / "app_root"
            account = Path(temp) / "wxid_a" / "db_storage"
            account.mkdir(parents=True)
            suffix = hashlib.sha256(str(account.resolve()).casefold().encode()).hexdigest()[:10]
            cache = app / "data" / f"wxid_a_{suffix}"
            snapshot = cache / "snapshot-test"
            (snapshot / "contact").mkdir(parents=True)
            with closing(sqlite3.connect(snapshot / "contact" / "contact.db")) as db:
                db.execute("CREATE TABLE contact(username TEXT)")
                db.commit()
            with patch.object(accounts, "__file__", str(app / "app" / "accounts.py")):
                self.assertIsNone(accounts.snapshot_for_account(account))
                (cache / "ready.json").write_text(json.dumps({"source": str(account.resolve()), "generation": "snapshot-test"}))
                self.assertEqual(accounts.snapshot_for_account(account), snapshot.resolve())


if __name__ == "__main__":
    unittest.main()
