"""Preview image messages from this account's local attachment cache only."""
from functools import lru_cache
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import struct
import tempfile
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image


def is_image_message(kind, content):
    if str(kind) == "3":
        return True
    if len(content) > 1024 * 1024 or "<img" not in content or "<!DOCTYPE" in content.upper():
        return False
    xml = content[content.find("<?xml"):] if "<?xml" in content else content[content.find("<msg") :]
    try:
        root = ET.fromstring(xml)
        return root.tag == "msg" and root.find("img") is not None
    except ET.ParseError:
        return False


def attachment_hash(packed):
    if not isinstance(packed, bytes):
        return ""
    matches = set(re.findall(rb"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", packed))
    return next(iter(matches)).decode() if len(matches) == 1 else ""


def decode_dat(data, key=None, xor_key=None):
    if data[:6] in (b"\x07\x08V1\x08\x07", b"\x07\x08V2\x08\x07"):
        if len(data) < 15:
            raise ValueError("图片缓存不完整")
        if data[3:4] == b"1":
            key = b"cfcd208495d565ef"
            if len(data) >= 2 and data[-2] ^ 255 == data[-1] ^ 217:
                xor_key = data[-1] ^ 217
        if key is None or xor_key is None:
            raise ValueError("暂未找到本地图片解密信息")
        aes_size, xor_size = struct.unpack_from("<II", data, 6)
        size = (aes_size // 16 + 1) * 16
        end = 15 + size
        if end > len(data) or xor_size > len(data) - end:
            raise ValueError("图片缓存长度异常")
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        plain = decryptor.update(data[15:end]) + decryptor.finalize()
        padding = plain[-1]
        if not 1 <= padding <= 16 or plain[-padding:] != bytes([padding]) * padding:
            raise ValueError("图片密钥校验失败")
        plain = plain[:-padding]
        if len(plain) != aes_size:
            raise ValueError("图片缓存长度异常")
        split = len(data) - xor_size
        return plain + data[end:split] + bytes(b ^ xor_key for b in data[split:])
    for magic in (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF8", b"RIFF"):
        if len(data) >= len(magic):
            value = data[0] ^ magic[0]
            if bytes(b ^ value for b in data[:len(magic)]) == magic:
                return bytes(b ^ value for b in data)
    return data


@lru_cache(maxsize=8)
def _local_keys(account_name):
    suffix = account_name.rsplit("_", 1)[-1]
    if not re.fullmatch(r"[0-9a-f]{4}", suffix):
        return ()
    clean = account_name.rsplit("_", 1)[0]
    base = Path(os.environ.get("APPDATA", "")) / "Tencent/xwechat"
    codes = set()
    for file in base.glob("net*/kvcomm/key_*.statistic"):
        match = re.match(r"key_(\d+)_", file.name)
        if match and hashlib.md5(match[1].encode()).hexdigest()[:4] == suffix:
            codes.add(match[1])
    return tuple((hashlib.md5((code + clean).encode()).hexdigest()[:16].encode(), int(code) & 255)
                 for code in codes)


def _account_for_snapshot(snapshot):
    snapshot = Path(snapshot).resolve()
    try:
        manifest = json.loads((snapshot.parent / "ready.json").read_text(encoding="utf-8"))
        if manifest.get("generation") != snapshot.name:
            return None
        root = Path(manifest["source"]).resolve()
        return root.parent if root.name.casefold() == "db_storage" else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def preview_message(snapshot, username, kind, content, packed):
    if not is_image_message(kind, content):
        return content, ""
    media_hash = attachment_hash(packed)
    if not media_hash:
        return "[图片：缺少本地附件索引]", ""
    account = _account_for_snapshot(snapshot)
    if account is None:
        return "[图片：当前账号附件目录不可用]", ""
    folder = account / "msg/attach" / hashlib.md5(username.encode()).hexdigest()
    candidates = []
    for suffix in (".dat", "_h.dat", "_t.dat", ".jpg", ".png"):
        candidates.extend(folder.glob("*/Img/" + media_hash + suffix))
    if not candidates:
        return "[图片未缓存到本机，请在微信中打开图片后重新读取]", ""
    cache = Path(snapshot).resolve().parent / "images"
    reason = "本地图片解码失败或格式暂不支持"
    for source in candidates:
        try:
            stat = source.stat()
            if stat.st_size > 40 * 1024 * 1024:
                reason = "图片超过预览大小限制"
                continue
            identity = f"{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
            target = cache / (hashlib.sha256(identity.encode()).hexdigest() + ".png")
            if target.is_file():
                return "[图片，点击查看大图]", str(target)
            data = source.read_bytes()
            keys = _local_keys(account.name) if data.startswith(b"\x07\x08V2") else ((None, None),)
            if not keys:
                reason = "暂未找到本地图片解密信息"
            for key, xor_key in keys:
                try:
                    decoded = decode_dat(data, key, xor_key)
                    with Image.open(BytesIO(decoded)) as image:
                        if image.width * image.height > 30000000:
                            raise ValueError("图片超过预览尺寸限制")
                        image.load()
                        cache.mkdir(parents=True, exist_ok=True)
                        with tempfile.NamedTemporaryFile(dir=cache, suffix=".tmp", delete=False) as temp:
                            temporary = Path(temp.name)
                        try:
                            image.convert("RGB").save(temporary, format="PNG")
                            os.replace(temporary, target)
                        finally:
                            temporary.unlink(missing_ok=True)
                    return "[图片，点击查看大图]", str(target)
                except (OSError, ValueError, Image.DecompressionBombError):
                    continue
        except (OSError, ValueError):
            continue
    return "[图片：" + reason + "]", ""
