"""Version 1 host services available to local recognizer plugins."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from app.snapshot_lock import snapshot_lock


def snapshot_text(value):
    if isinstance(value, bytes):
        if value.startswith(b"\x28\xb5\x2f\xfd"):
            try:
                import zstandard
                value = zstandard.ZstdDecompressor().decompress(value, max_output_size=1024 * 1024)
            except Exception:
                return ""
        return value.decode("utf-8", errors="replace")
    return str(value or "")


@contextmanager
def open_snapshot(path):
    path = Path(path).resolve()
    if "db_storage" in (part.casefold() for part in path.parts):
        raise ValueError("插件不得通过快照接口打开微信原始数据库")
    with snapshot_lock(path.parent.parent):
        from app.db_incremental import recover_pending
        recover_pending(path)
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            yield db
        finally:
            db.close()


def snapshot_stamp(path):
    def part(item):
        try:
            info = item.stat()
            return info.st_size, info.st_mtime_ns
        except FileNotFoundError:
            return None
    path = Path(path)
    return part(path), part(Path(str(path) + "-wal"))


def ocr_boxes(image):
    from app.ocr import _engine
    results, _ = _engine()(image, use_cls=False)
    return results or []


def classify_chat_box(image, box):
    from app.ocr import who_said
    return who_said(image, box)[0]
