"""Prepare private, account-scoped SQLite copies; never query or decrypt live WeChat DBs."""
from contextlib import closing
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import tempfile
import uuid

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.accounts import account_cache

PAGE = 4096
HEADER = b"SQLite format 3\0"


def _signature(path):
    def stat(item):
        try:
            info = item.stat()
            return info.st_size, info.st_mtime_ns
        except FileNotFoundError:
            return None
    return stat(path), stat(Path(str(path) + "-wal"))


def _copy_live_files(source, target, cancelled=None):
    """The only operation on live DBs is a byte-for-byte read/copy."""
    target.parent.mkdir(parents=True, exist_ok=True)
    def copy_bytes(origin, destination):
        if cancelled is None:
            shutil.copyfile(origin, destination)
            return
        with origin.open("rb") as src, destination.open("wb") as dst:
            while chunk := src.read(4 * 1024 * 1024):
                if cancelled():
                    raise InterruptedError("账号已切换。")
                dst.write(chunk)

    for _ in range(3):
        before = _signature(source)
        if before[0] is None:
            raise ValueError("数据库文件不存在。")
        copy_bytes(source, target)
        wal_source = Path(str(source) + "-wal")
        wal_target = Path(str(target) + "-wal")
        if wal_source.exists():
            try:
                copy_bytes(wal_source, wal_target)
            except FileNotFoundError:
                continue
        elif wal_target.exists():
            wal_target.unlink()
        if _signature(source) == before:
            return before
    raise ValueError("微信正在写入数据库，请稍后重试准备。")


def _checksum(data, endian, initial=(0, 0)):
    words = struct.unpack(endian + "I" * (len(data) // 4), data)
    a, b = initial
    for i in range(0, len(words), 2):
        a = (a + words[i] + b) & 0xffffffff
        b = (b + words[i + 1] + a) & 0xffffffff
    return a, b


def _committed_frames(wal):
    if not wal.exists():
        return [], 0
    with wal.open("rb") as stream:
        header = stream.read(32)
        if not header:
            return [], 0
        if len(header) < 32:
            raise ValueError("WAL 文件不完整，请稍后重试。")
        magic, version, page_size = struct.unpack(">III", header[:12])
        if magic not in (0x377f0682, 0x377f0683) or version != 3007000 or page_size != PAGE:
            raise ValueError("不支持此 WAL 格式。")
        endian = "<" if magic == 0x377f0682 else ">"
        rolling = _checksum(header[:24], endian)
        if rolling != struct.unpack(">II", header[24:32]):
            raise ValueError("WAL 头部校验失败。")
        count = committed = db_size = 0
        while len(frame := stream.read(24 + PAGE)) == 24 + PAGE:
            page_number, size = struct.unpack(">II", frame[:8])
            if not page_number or frame[8:16] != header[16:24]:
                break
            check = _checksum(frame[:8] + frame[24:], endian, rolling)
            if check != struct.unpack(">II", frame[16:24]):
                break
            rolling = check
            count += 1
            if size:
                committed, db_size = count, size

    def frames():
        with wal.open("rb") as stream:
            stream.seek(32)
            for _ in range(committed):
                frame = stream.read(24 + PAGE)
                yield struct.unpack(">I", frame[:4])[0], frame[24:]

    return frames(), db_size


def _mac_key(key, salt):
    return hashlib.pbkdf2_hmac("sha512", key, bytes(b ^ 0x3a for b in salt), 2, dklen=32)


def _valid_page(mac_key, value, number):
    if len(value) != PAGE:
        return False
    start = 16 if number == 1 else 0
    expected = hmac.new(mac_key, value[start:PAGE - 64] + struct.pack("<I", number), hashlib.sha512).digest()
    return hmac.compare_digest(expected, value[-64:])


def _decrypt_page(key, mac_key, value, number):
    if not _valid_page(mac_key, value, number):
        if number > 1 and not any(value):
            return value
        raise ValueError("数据库页校验失败，请重新准备。")
    iv = value[PAGE - 80:PAGE - 64]
    encrypted = value[16:PAGE - 80] if number == 1 else value[:PAGE - 80]
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = decryptor.update(encrypted) + decryptor.finalize()
    return (HEADER + plain if number == 1 else plain) + bytes(80)


def _decrypt_copy(source, target, key, cancelled=None):
    with source.open("rb") as stream:
        first = stream.read(PAGE)
    mac_key = _mac_key(key, first[:16])
    if not _valid_page(mac_key, first, 1):
        raise ValueError("数据库密钥不匹配。")
    with source.open("rb") as src, target.open("wb+") as dst:
        number = 0
        while value := src.read(PAGE):
            number += 1
            if cancelled is not None and number % 1024 == 0 and cancelled():
                raise InterruptedError("账号已切换。")
            dst.write(_decrypt_page(key, mac_key, value, number))
        frames, size = _committed_frames(Path(str(source) + "-wal"))
        for number, value in frames:
            dst.seek((number - 1) * PAGE)
            dst.write(_decrypt_page(key, mac_key, value, number))
        if size:
            dst.truncate(size * PAGE)
        dst.seek(18)
        dst.write(b"\x01\x01")


def _key_candidates(cache, supplied):
    files = [cache / "keys.json"]
    if supplied:
        files.append(Path(supplied))
    # Reuse an existing local key-tool export; this never invokes its scanner.
    files.extend(sorted((cache.parent.parent.parent / "wcdb-key-tool").glob("all_keys_*.json")))
    for file in files:
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            for item in data.values():
                if isinstance(item, dict):
                    try:
                        key = bytes.fromhex(item["enc_key"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if len(key) == 32:
                        yield key


def _matching_key(copied, candidates):
    with copied.open("rb") as stream:
        first = stream.read(PAGE)
    if len(first) != PAGE:
        raise ValueError("数据库副本不完整。")
    for key in candidates:
        if _valid_page(_mac_key(key, first[:16]), first, 1):
            return key
    raise ValueError("缺少该账号的数据库密钥；请选择已有密钥文件后重试。不会自动扫描进程内存。")


def _check_sqlite(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("数据库副本一致性检查失败。")


def prepare_database(root, key_file=None):
    """Copy first, then decrypt/query only copies; publish after every shard passes."""
    root = Path(root).resolve()
    cache = account_cache(root)
    cache.mkdir(parents=True, exist_ok=True)
    sources = [root / "contact" / "contact.db"] + sorted((root / "message").glob("message_[0-9]*.db"))
    session = root / "session" / "session.db"
    if session.is_file():
        sources.append(session)
    if not sources[0].is_file():
        raise ValueError("当前账号缺少 contact.db。")
    candidates = list(dict.fromkeys(_key_candidates(cache, key_file)))
    generation = "snapshot-" + uuid.uuid4().hex
    stage = cache / generation
    stage.mkdir()
    matched = {}
    sync = {}
    try:
        with tempfile.TemporaryDirectory(dir=cache) as copy_dir:
            copied_root = Path(copy_dir)
            for source in sources:
                relative = source.relative_to(root)
                copied = copied_root / relative
                output = stage / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                source_sig = _copy_live_files(source, copied)
                with copied.open("rb") as stream:
                    plaintext = stream.read(16) == HEADER
                if plaintext:
                    with closing(sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True)) as src:
                        with closing(sqlite3.connect(output)) as dst:
                            src.backup(dst)
                else:
                    key = _matching_key(copied, candidates)
                    _decrypt_copy(copied, output, key)
                    matched[str(relative).replace("/", "\\")] = {"enc_key": key.hex()}
                _check_sqlite(output)
                from app.db_incremental import initial_entry
                sync[relative.as_posix()] = initial_entry(copied, not plaintext, source_sig)
        if matched:
            temporary_keys = cache / "keys.json.tmp"
            temporary_keys.write_text(json.dumps(matched), encoding="utf-8")
            os.replace(temporary_keys, cache / "keys.json")
        (stage / "sync.json").write_text(json.dumps(sync), encoding="utf-8")
        manifest = {"source": str(root), "generation": generation}
        temporary = cache / "ready.json.tmp"
        temporary.write_text(json.dumps(manifest), encoding="utf-8")
        os.replace(temporary, cache / "ready.json")
        return stage
    except Exception:
        shutil.rmtree(stage)
        raise
