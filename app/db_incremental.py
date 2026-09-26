"""Apply committed WeChat WAL pages to local plaintext copies, never to source DBs."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import tempfile

from app.accounts import account_cache, snapshot_for_account
from app.db_snapshot import (PAGE, HEADER, _check_sqlite, _checksum, _copy_live_files,
                             _decrypt_copy, _decrypt_page, _mac_key, _matching_key, _signature,
                             _key_candidates)
from app.snapshot_lock import snapshot_lock

FRAME = 24 + PAGE
MAX_DELTA = 32 * 1024 * 1024
UNDO_MAGIC = b"JEVUNDO1"


def _atomic_json(path, value):
    temporary = Path(str(path) + ".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, path)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).digest()


def _wal_header(header):
    if len(header) != 32:
        raise ValueError("WAL 头部不完整。")
    magic, version, page_size = struct.unpack(">III", header[:12])
    if magic not in (0x377f0682, 0x377f0683) or version != 3007000 or page_size != PAGE:
        raise ValueError("WAL 格式不兼容。")
    endian = "<" if magic == 0x377f0682 else ">"
    initial = _checksum(header[:24], endian)
    if initial != struct.unpack(">II", header[24:32]):
        raise ValueError("WAL 头部校验失败。")
    return endian, initial


def wal_cursor(wal):
    """Cursor refers to the last *committed* frame, not an in-flight WAL tail."""
    if not wal.exists() or not wal.stat().st_size:
        return {"header": "", "frames": 0, "checksum": [0, 0], "db_size": 0}
    with wal.open("rb") as stream:
        header = stream.read(32)
        endian, rolling = _wal_header(header)
        committed = size = 0
        committed_sum = rolling
        count = 0
        while len(frame := stream.read(FRAME)) == FRAME:
            number, frame_size = struct.unpack(">II", frame[:8])
            if not number or frame[8:16] != header[16:24]:
                break
            check = _checksum(frame[:8] + frame[24:], endian, rolling)
            if check != struct.unpack(">II", frame[16:24]):
                break
            rolling = check
            count += 1
            if frame_size:
                committed, size, committed_sum = count, frame_size, rolling
    return {"header": header.hex(), "frames": committed,
            "checksum": list(committed_sum), "db_size": size}


def initial_entry(copied, encrypted, source_sig):
    with copied.open("rb") as stream:
        salt = stream.read(16).hex() if encrypted else ""
    return {"encrypted": encrypted, "salt": salt,
            "db_sig": list(source_sig[0]), "wal_sig": list(source_sig[1]) if source_sig[1] else None,
            "cursor": wal_cursor(Path(str(copied) + "-wal"))}


def _entry_hash(snapshot, path):
    metadata = json.loads((snapshot / "sync.json").read_text(encoding="utf-8"))
    return _digest(metadata[str(path.relative_to(snapshot)).replace("\\", "/")])


def recover_pending(path):
    """Called with snapshot_lock held, before every local SQLite read or update."""
    path = Path(path)
    snapshot = path.parent.parent
    undo = Path(str(path) + ".undo")
    if undo.exists():
        with undo.open("rb") as stream:
            if stream.read(8) != UNDO_MAGIC:
                raise ValueError("本地回滚日志损坏，请重新准备数据库。")
            old_size = struct.unpack("<Q", stream.read(8))[0]
            target_hash = stream.read(32)
            count = struct.unpack("<I", stream.read(4))[0]
            committed = False
            try:
                committed = _entry_hash(snapshot, path) == target_hash
            except (OSError, KeyError, ValueError):
                pass
            if not committed:
                with path.open("rb+") as db:
                    for _ in range(count):
                        number = struct.unpack("<Q", stream.read(8))[0]
                        value = stream.read(PAGE)
                        if len(value) != PAGE:
                            raise ValueError("本地回滚日志不完整，请重新准备数据库。")
                        db.seek((number - 1) * PAGE)
                        db.write(value)
                    db.truncate(old_size)
                    db.flush()
                    os.fsync(db.fileno())
        undo.unlink()
    marker = Path(str(path) + ".rebase.json")
    backup = Path(str(path) + ".rebase-old")
    if marker.exists():
        expected = bytes.fromhex(json.loads(marker.read_text(encoding="utf-8"))["target_hash"])
        try:
            committed = _entry_hash(snapshot, path) == expected
        except (OSError, KeyError, ValueError):
            committed = False
        if backup.exists():
            if committed:
                backup.unlink()
            else:
                os.replace(backup, path)
        marker.unlink()


def _copy_delta(source, old, destination):
    """Only copy source WAL bytes; parse and decrypt the local copies."""
    wal = Path(str(source) + "-wal")
    before = _signature(source)
    db_sig = list(before[0]) if before[0] else None
    wal_sig = list(before[1]) if before[1] else None
    if wal_sig is None or not wal_sig[0]:
        if old["cursor"]["header"] or db_sig != old["db_sig"]:
            return "rebase", None
        return "idle", None
    offset = 32 + old["cursor"]["frames"] * FRAME
    with wal.open("rb") as stream, destination.open("wb") as out:
        header_copy = Path(str(destination) + ".header")
        with header_copy.open("wb") as copied:
            copied.write(stream.read(32))
        header = header_copy.read_bytes()
        if len(header) < 32:
            return "retry", None
        _wal_header(header)
        if old["cursor"]["header"] and header.hex() != old["cursor"]["header"]:
            return "rebase", None
        if wal_sig == old["wal_sig"] and (old["cursor"]["header"] or db_sig == old["db_sig"]):
            return "idle", None
        if not old["cursor"]["header"] and db_sig != old["db_sig"]:
            return "rebase", None
        if wal_sig[0] < offset:
            return "rebase", None
        if old["cursor"]["frames"]:
            stream.seek(offset - FRAME + 16)
            prior_copy = Path(str(destination) + ".prior")
            with prior_copy.open("wb") as copied:
                copied.write(stream.read(8))
            previous = struct.unpack(">II", prior_copy.read_bytes())
            if previous != tuple(old["cursor"]["checksum"]):
                return "rebase", None
        stream.seek(offset)
        if wal_sig[0] - offset > MAX_DELTA:
            return "rebase", None
        shutil.copyfileobj(stream, out)
    if _signature(source) != before:
        return "retry", None
    return "delta", (header, db_sig, wal_sig)


def _parse_delta(delta, header, old_cursor):
    endian, initial = _wal_header(header)
    rolling = tuple(old_cursor["checksum"]) if old_cursor["header"] else initial
    count = committed = size = 0
    committed_sum = rolling
    with delta.open("rb") as stream:
        while len(frame := stream.read(FRAME)) == FRAME:
            number, frame_size = struct.unpack(">II", frame[:8])
            if not number or frame[8:16] != header[16:24]:
                break
            check = _checksum(frame[:8] + frame[24:], endian, rolling)
            if check != struct.unpack(">II", frame[16:24]):
                break
            rolling = check
            count += 1
            if frame_size:
                committed, size, committed_sum = count, frame_size, rolling
    if not committed:
        return None
    cursor = {"header": header.hex(), "frames": old_cursor["frames"] + committed,
              "checksum": list(committed_sum), "db_size": size}
    return committed, cursor


def _pages(delta, count, entry, key):
    mac_key = _mac_key(key, bytes.fromhex(entry["salt"])) if entry["encrypted"] else None
    changed = {}
    with delta.open("rb") as stream:
        for _ in range(count):
            frame = stream.read(FRAME)
            number = struct.unpack(">I", frame[:4])[0]
            value = (_decrypt_page(key, mac_key, frame[24:], number)
                     if entry["encrypted"] else frame[24:])
            if number == 1:
                value = value[:18] + b"\x01\x01" + value[20:]
            changed[number] = value
    return changed


def _write_undo(path, changed, target_hash):
    old_size = path.stat().st_size
    undo = Path(str(path) + ".undo")
    temporary = Path(str(undo) + ".tmp")
    originals = [number for number in changed if number * PAGE <= old_size]
    with path.open("rb") as db, temporary.open("wb") as stream:
        stream.write(UNDO_MAGIC + struct.pack("<Q", old_size) + target_hash + struct.pack("<I", len(originals)))
        for number in originals:
            db.seek((number - 1) * PAGE)
            stream.write(struct.pack("<Q", number) + db.read(PAGE))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, undo)


def _commit_pages(path, changed, target_size, metadata, relative, next_entry):
    if target_size * PAGE < path.stat().st_size:
        raise ValueError("数据库收缩，需要重同步分库。")
    _write_undo(path, changed, _digest(next_entry))
    try:
        with path.open("rb+") as db:
            for number, value in changed.items():
                db.seek((number - 1) * PAGE)
                db.write(value)
            db.truncate(target_size * PAGE)
            db.flush()
            os.fsync(db.fileno())
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        metadata[relative] = next_entry
        _atomic_json(path.parent.parent / "sync.json", metadata)
    except Exception:
        recover_pending(path)
        raise
    recover_pending(path)


def _rebase(source, target, cache, metadata, relative, cancelled):
    with tempfile.TemporaryDirectory(dir=target.parent.parent) as temp:
        copied = Path(temp) / "source.db"
        fresh = Path(temp) / "plain.db"
        source_sig = _copy_live_files(source, copied, cancelled)
        with copied.open("rb") as stream:
            encrypted = stream.read(16) != HEADER
        if encrypted:
            key = _matching_key(copied, list(dict.fromkeys(_key_candidates(cache, None))))
            cached = json.loads((cache / "keys.json").read_text(encoding="utf-8"))
            if cached.get(relative.replace("/", "\\"), {}).get("enc_key") != key.hex():
                raise ValueError("数据库密钥已变化，请重新准备当前账号数据库。")
            _decrypt_copy(copied, fresh, key, cancelled)
        else:
            with closing(sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True)) as src:
                with closing(sqlite3.connect(fresh)) as dst:
                    src.backup(dst)
        _check_sqlite(fresh)
        if cancelled():
            raise InterruptedError("账号已切换。")
        if encrypted != metadata[relative]["encrypted"]:
            raise ValueError("数据库加密状态已变化，请重新准备当前账号数据库。")
        next_entry = initial_entry(copied, encrypted, source_sig)
        marker = Path(str(target) + ".rebase.json")
        backup = Path(str(target) + ".rebase-old")
        with snapshot_lock(target.parent.parent):
            recover_pending(target)
            state_file = target.parent.parent / "sync.json"
            current = json.loads(state_file.read_text(encoding="utf-8"))
            if current.get(relative) != metadata[relative]:
                return False
            _atomic_json(marker, {"target_hash": _digest(next_entry).hex()})
            os.replace(target, backup)
            os.replace(fresh, target)
            current[relative] = next_entry
            _atomic_json(state_file, current)
            recover_pending(target)
            metadata[relative] = next_entry
            return True


def _ensure_session_snapshot(root, snapshot, metadata, cancelled):
    relative = "session/session.db"
    source = root / relative
    if relative in metadata or not source.is_file() or cancelled():
        return metadata
    with tempfile.TemporaryDirectory(dir=snapshot) as temp:
        copied = Path(temp) / "source.db"
        plain = Path(temp) / "plain.db"
        key = None
        source_sig = _copy_live_files(source, copied, cancelled)
        with copied.open("rb") as stream:
            encrypted = stream.read(16) != HEADER
        if encrypted:
            key = _matching_key(copied, list(dict.fromkeys(_key_candidates(account_cache(root), None))))
            _decrypt_copy(copied, plain, key, cancelled)
        else:
            with closing(sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True)) as src:
                with closing(sqlite3.connect(plain)) as dst:
                    src.backup(dst)
        _check_sqlite(plain)
        if cancelled():
            return metadata
        entry = initial_entry(copied, encrypted, source_sig)
        with snapshot_lock(snapshot):
            current = json.loads((snapshot / "sync.json").read_text(encoding="utf-8"))
            if relative not in current:
                if key is not None:
                    key_file = account_cache(root) / "keys.json"
                    try:
                        keys = json.loads(key_file.read_text(encoding="utf-8"))
                    except FileNotFoundError:
                        keys = {}
                    keys[relative.replace("/", "\\")] = {"enc_key": key.hex()}
                    _atomic_json(key_file, keys)
                target = snapshot / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(plain, target)
                current[relative] = entry
                _atomic_json(snapshot / "sync.json", current)
            return current


def sync_account(root, snapshot, username="", cancelled=lambda: False, progress=lambda _: None):
    """Poll one account; return changed relative DB paths and whether a slow rebase ran."""
    root = Path(root).resolve()
    snapshot = Path(snapshot).resolve()
    if snapshot_for_account(root) != snapshot:
        return [], False
    state_file = snapshot / "sync.json"
    if not state_file.is_file():
        raise ValueError("旧快照不支持增量更新，请重新准备数据库。")
    metadata = json.loads(state_file.read_text(encoding="utf-8"))
    with snapshot_lock(snapshot):
        for relative in metadata:
            recover_pending(snapshot / relative)
        metadata = json.loads(state_file.read_text(encoding="utf-8"))
    metadata = _ensure_session_snapshot(root, snapshot, metadata, cancelled)
    known = {relative for relative in metadata if relative.startswith("message/")}
    current = {path.relative_to(root).as_posix() for path in (root / "message").glob("message_[0-9]*.db")}
    if current != known:
        raise ValueError("消息分库已增减，请重新准备当前账号数据库。")
    active = set(metadata)
    if username:
        from app.plugin_api import open_snapshot as _open
        table = "Msg_" + hashlib.md5(username.encode()).hexdigest()
        active = {"contact/contact.db", "session/session.db"}
        for relative in known:
            with _open(snapshot / relative) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    active.add(relative)
    cache = account_cache(root)
    changed = []
    rebased = False
    for relative, entry in list(metadata.items()):
        if relative not in active:
            continue
        if cancelled():
            return changed, rebased
        source = root / relative
        target = snapshot / relative
        if not source.is_file() or not target.is_file():
            raise ValueError("数据库分库已变化，请重新准备数据库。")
        with tempfile.TemporaryDirectory(dir=snapshot) as temp:
            delta = Path(temp) / "delta.wal"
            action, info = _copy_delta(source, entry, delta)
            if action in ("idle", "retry"):
                continue
            if action == "rebase":
                if cancelled():
                    return changed, rebased
                progress("rebase")
                if not _rebase(source, target, cache, metadata, relative, cancelled):
                    continue
                changed.append(relative)
                rebased = True
                continue
            header, _db_sig, wal_sig = info
            parsed = _parse_delta(delta, header, entry["cursor"])
            if parsed is None:
                continue
            count, cursor = parsed
            if cursor["db_size"] * PAGE < target.stat().st_size:
                if cancelled():
                    return changed, rebased
                progress("rebase")
                if not _rebase(source, target, cache, metadata, relative, cancelled):
                    continue
                changed.append(relative)
                rebased = True
                continue
            key = b""
            if entry["encrypted"]:
                key_info = json.loads((cache / "keys.json").read_text(encoding="utf-8"))
                key = bytes.fromhex(key_info[relative.replace("/", "\\")]["enc_key"])
            pages = _pages(delta, count, entry, key)
            if cancelled():
                return changed, rebased
            next_entry = {**entry, "cursor": cursor, "wal_sig": wal_sig}
            with snapshot_lock(snapshot):
                recover_pending(target)
                current = json.loads(state_file.read_text(encoding="utf-8"))
                if current.get(relative) != entry:
                    continue
                _commit_pages(target, pages, cursor["db_size"], current, relative, next_entry)
                metadata[relative] = next_entry
            changed.append(relative)
    return changed, rebased


def _refresh_legacy_copy(root, snapshot, relative, cancelled):
    """Re-copy one legacy metadata DB without reading its source through SQLite."""
    root, snapshot = Path(root).resolve(), Path(snapshot).resolve()
    if snapshot_for_account(root) != snapshot or (snapshot / "sync.json").is_file():
        return False
    source = root / relative
    if not source.is_file() or cancelled():
        return False
    target = snapshot / relative
    state = snapshot / ("legacy_" + source.parent.name + ".json")
    signature = [list(item) if item else None for item in _signature(source)]
    try:
        previous = json.loads(state.read_text(encoding="utf-8"))["source_sig"]
    except (OSError, ValueError, KeyError):
        previous = None
    if target.is_file() and signature == previous:
        return False
    with tempfile.TemporaryDirectory(dir=snapshot) as temp:
        copied = Path(temp) / "source.db"
        plain = Path(temp) / "plain.db"
        source_sig = _copy_live_files(source, copied, cancelled)
        with copied.open("rb") as stream:
            encrypted = stream.read(16) != HEADER
        if encrypted:
            cache = account_cache(root)
            key = _matching_key(copied, list(dict.fromkeys(_key_candidates(cache, None))))
            _decrypt_copy(copied, plain, key, cancelled)
        else:
            with closing(sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True)) as src:
                with closing(sqlite3.connect(plain)) as dst:
                    src.backup(dst)
        _check_sqlite(plain)
        if cancelled():
            return False
        with snapshot_lock(snapshot):
            if snapshot_for_account(root) != snapshot or cancelled():
                return False
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(plain, target)
            _atomic_json(state, {"source_sig": [list(item) if item else None for item in source_sig]})
    return True


def refresh_legacy_session(root, snapshot, cancelled=lambda: False):
    return _refresh_legacy_copy(root, snapshot, "session/session.db", cancelled)


def refresh_legacy_contact(root, snapshot, cancelled=lambda: False):
    return _refresh_legacy_copy(root, snapshot, "contact/contact.db", cancelled)
