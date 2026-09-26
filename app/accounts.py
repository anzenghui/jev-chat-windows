"""Map a WeChat window to its database account using read-only file-handle evidence."""
import ctypes
import hashlib
import json
from pathlib import Path
import queue
import sys
import threading
import time

import psutil

_WECHAT_EXES = ("weixin.exe", "wechat.exe")


def wechat_root_process(pid):
    try:
        process = psutil.Process(pid)
        if process.name().lower() not in _WECHAT_EXES:
            return None
        while True:
            parent = process.parent()
            if parent is None or parent.name().lower() not in _WECHAT_EXES:
                return process
            process = parent
    except (psutil.Error, OSError):
        return None


def _window_pid(hwnd):
    pid = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _database_root(filename):
    path = Path(filename)
    if path.suffix.lower() not in (".db", ".db-wal", ".db-shm"):
        return None
    for parent in path.parents:
        if parent.name.lower() == "db_storage":
            return parent if (parent / "contact" / "contact.db").is_file() else None
    return None


def account_for_window(hwnd):
    """Return a unique account root, never one inferred from directory timestamps or OCR."""
    try:
        window_process = psutil.Process(_window_pid(hwnd))
        if window_process.name().lower() not in _WECHAT_EXES:
            return None
        def opened_roots(item):
            roots = set()
            for opened in item.open_files():
                root = _database_root(opened.path)
                if root:
                    roots.add(root.resolve())
            return roots

        roots = opened_roots(window_process)
        if roots:
            return next(iter(roots)) if len(roots) == 1 else None
        process = wechat_root_process(window_process.pid)
        if process is None:
            return None
        for item in [process] + process.children(recursive=True):
            if item.pid != window_process.pid:
                roots.update(opened_roots(item))
        return next(iter(roots)) if len(roots) == 1 else None
    except (psutil.Error, OSError):
        return None


class AccountProbe:
    """Recheck window ownership off the OCR path without accepting ambiguous roots."""

    def __init__(self, hwnd, interval=10.0, lookup=None):
        self.hwnd = hwnd
        self.interval = interval
        self.lookup = lookup or account_for_window
        self.results = queue.Queue()
        self.generation = 0
        self.running = False
        self.ready = False
        self.root = None
        self.revision = 0
        self.next_check = 0.0

    def reset(self):
        self.generation += 1
        self.ready = False
        self.root = None
        self.revision += 1
        self.next_check = 0.0

    def poll(self):
        while True:
            try:
                generation, root = self.results.get_nowait()
            except queue.Empty:
                break
            if generation != self.generation:
                self.running = False
                continue
            self.running = False
            self.next_check = time.monotonic() + (self.interval if root else 2.0)
            if not self.ready or root != self.root:
                self.root = root
                self.revision += 1
            self.ready = True
        if not self.running and time.monotonic() >= self.next_check:
            self.running = True
            generation = self.generation

            def work():
                try:
                    root = self.lookup(self.hwnd)
                except Exception:
                    root = None
                self.results.put((generation, root))

            threading.Thread(target=work, daemon=True).start()
        return self.root if self.ready else None


def account_cache(root):
    root = Path(root).resolve()
    name = root.parent.name + "_" + hashlib.sha256(str(root).casefold().encode()).hexdigest()[:10]
    app_root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    return app_root / "data" / name


def snapshot_for_account(root):
    """Return only a published local copy for this exact account."""
    if root is None:
        return None
    root = Path(root).resolve()
    cache = account_cache(root)
    try:
        manifest = json.loads((cache / "ready.json").read_text(encoding="utf-8"))
        if manifest.get("source") != str(root):
            return None
        generation = manifest["generation"]
        if not isinstance(generation, str) or not generation.startswith("snapshot-") or "/" in generation or "\\" in generation:
            return None
        candidate = cache / generation
        with (candidate / "contact" / "contact.db").open("rb") as stream:
            return candidate.resolve() if stream.read(16) == b"SQLite format 3\0" else None
    except (OSError, ValueError, KeyError, TypeError):
        return None
