"""Page a confirmed conversation from published, read-only message snapshots."""
import hashlib
from pathlib import Path
import sqlite3

from app.plugin_api import open_snapshot as _open, snapshot_text as _text


def read_page(snapshot, username, before=None, limit=50):
    snapshot = Path(snapshot).resolve()
    table = "Msg_" + hashlib.md5(username.encode()).hexdigest()
    newest = []
    for path in sorted((snapshot / "message").glob("message_[0-9]*.db")):
        shard = path.name
        try:
            with _open(path) as db:
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                if not exists:
                    continue
                fields = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
                if not {"create_time", "message_content"}.issubset(fields):
                    continue
                sender_column = "real_sender_id" if "real_sender_id" in fields else "NULL AS real_sender_id"
                condition, params = "", []
                if before:
                    stamp, old_shard, rowid = before
                    if shard < old_shard:
                        condition, params = "WHERE create_time <= ?", [stamp]
                    elif shard == old_shard:
                        condition, params = "WHERE create_time < ? OR (create_time = ? AND rowid < ?)", [stamp, stamp, rowid]
                    else:
                        condition, params = "WHERE create_time < ?", [stamp]
                rows = db.execute(
                    f'SELECT rowid AS _rowid,create_time,message_content,{sender_column} FROM "{table}" '
                    f'{condition} ORDER BY create_time DESC,rowid DESC LIMIT ?', (*params, limit))
                senders = {}
                has_names = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='Name2Id'").fetchone()
                for row in rows:
                    sender_id = row["real_sender_id"]
                    if sender_id is not None and sender_id not in senders and has_names:
                        name = db.execute("SELECT user_name FROM Name2Id WHERE rowid=?", (sender_id,)).fetchone()
                        senders[sender_id] = _text(name[0]) if name else ""
                    content = _text(row["message_content"])
                    newest.append((row["create_time"], shard, row["_rowid"], senders.get(sender_id, ""),
                                   content or "[非文本消息]"))
        except (OSError, sqlite3.Error):
            continue
    newest.sort(key=lambda item: item[:3], reverse=True)
    page = newest[:limit]
    cursor = page[-1][:3] if page else before
    return list(reversed(page)), cursor, len(page) == limit
