"""Page a confirmed conversation from published, read-only message snapshots."""
import hashlib
from pathlib import Path
import sqlite3
from xml.etree import ElementTree

from app.plugin_api import open_snapshot as _open, snapshot_text as _text
from app.plugin_loader import load_plugin


def _display_content(content, local_type):
    kind = str(local_type)
    if kind == "1":
        return content or "[空消息]"
    if kind == "50":
        try:
            detail = ElementTree.fromstring(content).findtext(".//VoIPBubbleMsg/msg") or ""
        except ElementTree.ParseError:
            detail = ""
        detail = " ".join(detail.split())[:80]
        return f"[通话] {detail}" if detail else "[通话]"
    labels = {"34": "[语音]", "43": "[视频]", "47": "[表情]", "49": "[分享或文件]"}
    if kind in labels:
        return labels[kind]
    if content.lstrip().startswith(("<?xml", "<msg", "<voipmsg")):
        return "[非文本消息]"
    return content or "[非文本消息]"


def read_page(snapshot, username, before=None, limit=50):
    snapshot = Path(snapshot).resolve()
    table = "Msg_" + hashlib.md5(username.encode()).hexdigest()
    newest = []
    media = None
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
                kind_column = "local_type" if "local_type" in fields else "NULL AS local_type"
                packed_column = "packed_info_data" if "packed_info_data" in fields else "NULL AS packed_info_data"
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
                    f'SELECT rowid AS _rowid,create_time,message_content,{sender_column},'
                    f'{kind_column},{packed_column} FROM "{table}" '
                    f'{condition} ORDER BY create_time DESC,rowid DESC LIMIT ?', (*params, limit))
                senders = {}
                has_names = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='Name2Id'").fetchone()
                for row in rows:
                    sender_id = row["real_sender_id"]
                    if sender_id is not None and sender_id not in senders and has_names:
                        name = db.execute("SELECT user_name FROM Name2Id WHERE rowid=?", (sender_id,)).fetchone()
                        senders[sender_id] = _text(name[0]) if name else ""
                    content = _text(row["message_content"])
                    image_path = ""
                    if str(row["local_type"]) == "3" or ("<msg" in content and "<img" in content):
                        if media is None:
                            try:
                                media = load_plugin("chat_media", "media_preview")
                            except (OSError, ValueError, ImportError):
                                media = False
                        if media:
                            try:
                                content, image_path = media.preview_message(
                                    snapshot, username, row["local_type"], content, row["packed_info_data"])
                            except Exception:
                                content = "[图片：本地预览失败]"
                        else:
                            content = "[图片：预览插件不可用]"
                    else:
                        content = _display_content(content, row["local_type"])
                    newest.append((row["create_time"], shard, row["_rowid"], senders.get(sender_id, ""),
                                   content or "[非文本消息]", image_path))
        except (OSError, sqlite3.Error):
            continue
    newest.sort(key=lambda item: item[:3], reverse=True)
    page = newest[:limit]
    cursor = page[-1][:3] if page else before
    return list(reversed(page)), cursor, len(page) == limit
