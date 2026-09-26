"""Resolve a window title against one account's read-only WeChat snapshots."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
from pathlib import Path
import re
import sqlite3
import time

import numpy as np

from app.plugin_api import classify_chat_box, ocr_boxes, open_snapshot, snapshot_stamp, snapshot_text


@dataclass(frozen=True)
class HeaderObservation:
    title: str = ""
    member_count: int | None = None
    truncated: bool = False


@dataclass(frozen=True)
class WindowObservation:
    header: HeaderObservation = HeaderObservation()
    visible: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    selection_token: str = ""


def choose_uia_header(controls):
    """Use only the selected window's header, never a sidebar display name alone."""
    names = {name.strip() for aid, name, _ in controls
             if aid.endswith(".current_chat_name_label") and name.strip()}
    if len(names) != 1:
        return HeaderObservation()
    title = names.pop()
    selected = {aid.split("session_item_", 1)[1] for aid, _, active in controls
                if active and "session_item_" in aid}
    if selected and (len(selected) != 1 or _title_equivalent(title) not in
                     {_title_equivalent(name) for name in selected}):
        return HeaderObservation()
    counts = {int(match[1]) for aid, name, _ in controls
              if aid.endswith(".current_chat_count_label")
              if (match := re.fullmatch(r"\s*[（(](\d{1,5})[)）]\s*", name))}
    if len(counts) > 1:
        return HeaderObservation()
    return HeaderObservation(title, next(iter(counts)) if counts else None)


def read_window_context(hwnd):
    """Read this window's title and visible message list without changing its state."""
    try:
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread():
            root = auto.ControlFromHandle(hwnd)
            pending = [(root, 0)]
            controls, visible, labels = [], [], []
            title_controls = []
            selected_tokens = []
            visited = 0
            while pending and visited < 350:
                control, depth = pending.pop(0)
                visited += 1
                aid = control.AutomationId or ""
                if aid.endswith((".current_chat_name_label", ".current_chat_count_label")):
                    controls.append((aid, control.Name or "", False))
                    if aid.endswith(".current_chat_name_label"):
                        title_controls.append(control)
                if aid == "session_list":
                    for item in control.GetChildren():
                        try:
                            selected = bool(item.GetSelectionItemPattern().IsSelected)
                        except Exception:
                            selected = False
                        if selected:
                            controls.append((item.AutomationId or "", "", True))
                            try:
                                selected_tokens.append(tuple(item.GetRuntimeId()))
                            except Exception:
                                pass
                    continue
                if aid == "chat_message_list":
                    for item in control.GetChildren()[:60]:
                        name = (item.Name or "").strip()
                        item_id = item.AutomationId or ""
                        if not name:
                            continue
                        if "chat_bubble_item_view" in item_id:
                            visible.append(name)
                        elif re.search(r"\d{1,2}月\d{1,2}日\s*\d{1,2}[:：]\d{2}", name):
                            labels.append(name)
                    continue
                if depth < 24 and aid not in ("chat_message_list", "chat_input_field"):
                    pending.extend((child, depth + 1) for child in control.GetChildren())
            header = choose_uia_header(controls)
            if header.title and any((control.Name or "").strip() != header.title
                                    for control in title_controls):
                return WindowObservation()
            token = (hashlib.blake2b(repr(selected_tokens[0]).encode(), digest_size=12).hexdigest()
                     if len(selected_tokens) == 1 else "")
            return WindowObservation(header, tuple(visible), tuple(labels), token) if header.title else WindowObservation()
    except Exception:
        return WindowObservation()


def read_window_header(hwnd):
    return read_window_context(hwnd).header


def read_header(header):
    return choose_header_boxes(ocr_boxes(header), header.shape[1], header.shape[0])


def read_title(header):
    """Recognize only the complete conversation-title row of this window."""
    return read_header(header).title


def choose_title_boxes(results, width, height):
    return choose_header_boxes(results, width, height).title


def choose_header_boxes(results, width, height):
    boxes = []
    for polygon, value, _ in results:
        xs, ys = zip(*polygon)
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        value = value.strip()
        if (not value or value.startswith(("@", "＠")) or x0 >= width * 0.8
                or y0 >= height * 0.55 or y1 - y0 < height * 0.08):
            continue
        boxes.append((x0, x1, y0, y1, value))
    if not boxes:
        return HeaderObservation()
    first = min(boxes, key=lambda box: (box[2], box[0]))
    mid = (first[2] + first[3]) / 2
    tolerance = max(5, (first[3] - first[2]) * 0.6)
    row = sorted((box for box in boxes if abs((box[2] + box[3]) / 2 - mid) <= tolerance),
                 key=lambda box: box[0])
    if len(row) != len(boxes):
        return HeaderObservation()
    fragments = [row[0][4]]
    right = row[0][1]
    for x0, x1, y0, y1, value in row[1:]:
        if x0 - right > max(12, (y1 - y0) * 1.5):
            break
        fragments.append(value)
        right = max(right, x1)
    text = "".join(fragments).strip()
    text = re.split(r"\s+[@＠]", text, maxsplit=1)[0]
    if re.search(r"[\u3400-\u9fff]", text):
        text = re.sub(r"\s+", "", text)
    count_match = re.search(r"\s*[（(](\d{1,5})[)）](?:\s*[A-Za-z*])?\s*$", text)
    member_count = int(count_match[1]) if count_match else None
    if count_match:
        text = text[:count_match.start()].strip()
    truncated = bool(re.search(r"(?:\.{1,3}|…|···)$", text))
    if not text or len(text) >= 100 or text in ("搜索", "微信", "Weixin", "Search"):
        return HeaderObservation()
    return HeaderObservation(text, member_count, truncated)


def read_identity_evidence(chat):
    """Upscale tiny bubbles only when duplicate names need message corroboration."""
    width = chat.shape[1]
    best = ([], [])
    for region in (chat[:, width // 5:], chat[:, :width * 4 // 5]):
        enlarged = np.repeat(np.repeat(region, 2, axis=0), 2, axis=1)
        lines, labels = [], []
        for box, value, _ in sorted(ocr_boxes(enlarged), key=lambda item: item[0][0][1]):
            kind = classify_chat_box(enlarged, box)
            if kind in ("me", "her"):
                lines.append(value)
            elif kind == "gray":
                labels.append(value)
        if len(lines) > len(best[0]):
            best = lines, labels
        if len(lines) >= 3:
            break
    return best


def _norm(value):
    return re.sub(r"\s+", "", value).strip()


def _fold_keycaps(value):
    # OCR commonly reads the visible digit but drops the keycap's variation marks.
    return re.sub(r"[\ufe0e\ufe0f]?\u20e3", "", value)


_TRAILING_EMOJI = re.compile(
    r"(?:[\U0001f000-\U0001faff\u2600-\u27bf][\ufe0e\ufe0f\u200d\U0001f3fb-\U0001f3ff]*)+$")


def _title_equivalent(value):
    value = _fold_keycaps(value)
    return _norm(_TRAILING_EMOJI.sub("", value))


def _ocr_l_key(value):
    return re.sub(r"[Il1]+", "l", _title_equivalent(value))


@dataclass(frozen=True)
class Match:
    title: str
    username: str = ""
    reason: str = ""
    ambiguous: bool = False

    @property
    def confirmed(self):
        return bool(self.username)


class SessionSignal:
    """Only a newly changed clear-unread value can corroborate this window's switch."""

    def __init__(self, directory=""):
        self.path = Path(directory) / "session" / "session.db" if directory else None
        self.stamp = None
        self.clear_times = None
        self.deadline = 0.0
        self.switch_wall = 0.0
        self.username = ""

    def has_update(self):
        return self.path is not None and self.path.is_file() and snapshot_stamp(self.path) != self.stamp

    def observe(self, switched=False, now=None, wall=None):
        now = time.monotonic() if now is None else now
        wall = time.time() if wall is None else wall
        if switched:
            self.deadline = now + 4.0
            self.switch_wall = wall
            self.username = ""
        if self.path is None or not self.path.is_file():
            return ""
        stamp = snapshot_stamp(self.path)
        if stamp != self.stamp:
            try:
                with open_snapshot(self.path) as db:
                    rows = db.execute(
                        "SELECT username, last_clear_unread_timestamp FROM SessionTable")
                    current = {snapshot_text(row[0]): int(row[1] or 0) for row in rows}
            except (OSError, sqlite3.Error, ValueError, KeyError):
                return ""
            if self.clear_times is not None and now <= self.deadline:
                changed = [user for user, value in current.items()
                           if value > self.clear_times.get(user, 0)
                           and self.switch_wall - 2 <= value <= wall + 1]
                self.username = changed[0] if len(changed) == 1 else ""
            self.clear_times, self.stamp = current, stamp
        return self.username if now <= self.deadline else ""


class Resolver:
    def __init__(self, directory=""):
        self.root = Path(directory).expanduser().resolve() if directory else None
        self._contacts_stamp = None
        self._contacts = []
        self._title_candidates = {}
        self._group_title_index = {}
        self._group_names = []
        self._group_prefix_cache = {}
        self._messages = {}
        self._sequences = {}
        self._member_count_stamp = None
        self._member_counts = {}

    def _contacts_for_title(self, title):
        if self.root is None:
            return None
        path = self.root / "contact" / "contact.db"
        try:
            stamp = snapshot_stamp(path)
            if stamp[0] is None:
                return None
            if stamp != self._contacts_stamp:
                with open_snapshot(path) as db:
                    columns = {r[1] for r in db.execute('PRAGMA table_info("contact")')}
                    if "username" not in columns:
                        return None
                    names = [n for n in ("username", "nick_name", "remark", "alias") if n in columns]
                    rows = db.execute("SELECT " + ",".join('"' + n + '"' for n in names) + " FROM contact")
                    self._contacts = [(snapshot_text(r["username"]), {snapshot_text(r[n]) for n in names}) for r in rows]
                self._title_candidates.clear()
                self._group_title_index.clear()
                self._group_prefix_cache.clear()
                self._group_names.clear()
                for user, names in self._contacts:
                    if user.endswith("@chatroom"):
                        for name in names:
                            if name:
                                self._group_title_index.setdefault(_ocr_l_key(name), set()).add(user)
                                self._group_names.append((user, name))
                self._contacts_stamp = stamp
            if title not in self._title_candidates:
                folded = _title_equivalent(title)
                self._title_candidates[title] = list(dict.fromkeys(
                    user for user, names in self._contacts if user and any(
                        title == name or (folded and _title_equivalent(name) == folded
                                          and (folded != title or _title_equivalent(name) != name))
                        for name in names)))
            return list(self._title_candidates[title])
        except (OSError, sqlite3.Error):
            self._contacts_stamp = None
            return None

    def _group_member_count(self, username):
        if self._member_count_stamp != self._contacts_stamp:
            self._member_count_stamp = self._contacts_stamp
            self._member_counts.clear()
        if username not in self._member_counts:
            try:
                with open_snapshot(self.root / "contact" / "contact.db") as db:
                    room = db.execute("SELECT id FROM chat_room WHERE username=?", (username,)).fetchone()
                    count = (db.execute("SELECT COUNT(*) FROM chatroom_member WHERE room_id=?",
                                        (room[0],)).fetchone()[0] if room else None)
            except (OSError, sqlite3.Error, ValueError):
                count = None
            self._member_counts[username] = count
        return self._member_counts[username]

    def _groups_with_prefix(self, title):
        prefix = _title_equivalent(title.rstrip(".…· "))
        if len(prefix) < 4:
            return set()
        if prefix not in self._group_prefix_cache:
            self._group_prefix_cache[prefix] = {
                user for user, name in self._group_names if _title_equivalent(name).startswith(prefix)}
        return self._group_prefix_cache[prefix]

    def _recent_text(self, username):
        table = "Msg_" + hashlib.md5(username.encode()).hexdigest()
        paths = sorted((self.root / "message").glob("message_[0-9]*.db"))
        try:
            stamp = tuple((str(path), snapshot_stamp(path)) for path in paths)
        except OSError:
            return set()
        cached = self._messages.get(username)
        if cached and cached[0] == stamp:
            return cached[1]
        found = []
        failed = False
        for path in paths:
            try:
                with open_snapshot(path) as db:
                    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                    if not exists:
                        continue
                    rows = db.execute(f'SELECT message_content FROM "{table}" ORDER BY create_time DESC LIMIT 200')
                    found.extend(_norm(snapshot_text(row[0])) for row in rows)
            except sqlite3.Error:
                failed = True
                continue
        result = {item for item in found if item}
        if not failed:
            self._messages[username] = (stamp, result)
        return result

    def _recent_sequence(self, username):
        table = "Msg_" + hashlib.md5(username.encode()).hexdigest()
        paths = sorted((self.root / "message").glob("message_[0-9]*.db"))
        stamp = tuple((str(path), snapshot_stamp(path)) for path in paths)
        cached = self._sequences.get(username)
        if cached and cached[0] == stamp:
            return cached[1]
        found = []
        try:
            for path in paths:
                with open_snapshot(path) as db:
                    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                        continue
                    rows = db.execute(
                        f'SELECT create_time,rowid,message_content FROM "{table}" '
                        'ORDER BY create_time DESC,rowid DESC LIMIT 200')
                    found.extend((int(row[0]), int(row[1]), _norm(snapshot_text(row[2]))) for row in rows)
        except (OSError, sqlite3.Error, ValueError):
            return None
        found.sort(key=lambda row: row[:2])
        self._sequences[username] = stamp, found
        return found

    def _sequence_match(self, username, visible, dates):
        rows = self._recent_sequence(username)
        if rows is None:
            return None
        width = len(visible)
        for start in range(len(rows) - width + 1):
            segment = rows[start:start + width]
            if [text for _, _, text in segment] != visible:
                continue
            stamp = datetime.fromtimestamp(segment[0][0])
            if (stamp.month, stamp.day, stamp.hour, stamp.minute) in dates:
                return True
        return False

    def candidate_options(self, title, member_count=None, truncated=False):
        candidates = self._contacts_for_title(title)
        if candidates is None:
            return []
        if member_count is not None:
            key = _ocr_l_key(title)
            similar = set(self._group_title_index.get(key, ()))
            if truncated:
                similar.update(self._groups_with_prefix(title))
            candidates = [user for user in dict.fromkeys(candidates + sorted(similar))
                          if user.endswith("@chatroom") and self._group_member_count(user) == member_count]
        if len(candidates) < 2:
            return []
        names_by_user = {user: names for user, names in self._contacts}
        return [(user, " / ".join(sorted(name for name in names_by_user.get(user, ())
                                        if name and name not in (title, user))[:2]) or user)
                for user in candidates]

    def resolve(self, title, visible=(), recent_username="", time_labels=(),
                member_count=None, truncated=False):
        title = title.strip()
        if not title:
            return Match("", reason="标题未识别")
        candidates = self._contacts_for_title(title)
        if candidates is None:
            return Match(title, reason="未配置可读取的联系人快照")
        if member_count is not None:
            key = _ocr_l_key(title)
            similar = set(self._group_title_index.get(key, ()))
            if truncated:
                similar.update(self._groups_with_prefix(title))
            possible = list(dict.fromkeys(candidates + sorted(similar)))
            candidates = [user for user in possible if user.endswith("@chatroom")
                          and self._group_member_count(user) == member_count]
            if not candidates:
                return Match(title, reason="群标题或成员数与联系人快照不符")
        if len(candidates) == 1:
            if recent_username and recent_username != candidates[0]:
                return Match(title, reason="标题与本次清未读记录指向不同会话")
            return Match(title, candidates[0])
        if not candidates:
            return Match(title, reason="联系人快照中无匹配")
        visible = [_norm(text) for text in visible if _norm(text)]
        evidence = {text for text in visible if len(text) >= 4}
        if evidence:
            scores = {user: len(evidence & self._recent_text(user)) for user in candidates}
            winners = [user for user, score in scores.items() if score >= 2]
            if len(winners) == 1 and all(scores[other] == 0 for other in candidates if other != winners[0]):
                return Match(title, winners[0])
            if (recent_username in candidates and scores[recent_username] >= 1
                    and all(scores[other] == 0 for other in candidates if other != recent_username)):
                return Match(title, recent_username)
        if len(visible) >= 3 and len(set(visible)) >= 2:
            dates = set()
            for label in time_labels:
                match = re.search(r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2})[:：](\d{2})", label)
                if match:
                    dates.add(tuple(map(int, match.groups())))
            if dates:
                hits = {user: self._sequence_match(user, visible, dates) for user in candidates}
                if all(hit is not None for hit in hits.values()):
                    winners = [user for user, hit in hits.items() if hit]
                    if len(winners) == 1:
                        return Match(title, winners[0])
        if not evidence:
            return Match(title, reason="同名或近似会话，待更多消息核对", ambiguous=True)
        return Match(title, reason="同名或近似会话，消息证据不唯一", ambiguous=True)


class Recognition:
    """Plugin-owned recognition policy and per-window evidence cache."""

    def __init__(self, directory=""):
        self.resolver = Resolver(directory)
        self.session_signal = SessionSignal(directory)
        self.identity_frame = None
        self.identity_evidence = ([], [])
        self._baseline_probe_stamp = None

    def needs_refresh(self):
        signal = self.session_signal
        if signal.stamp is None and signal.path is not None and signal.path.is_file():
            stamp = snapshot_stamp(signal.path)
            if stamp != self._baseline_probe_stamp:
                self._baseline_probe_stamp = stamp
                return True
        return signal.deadline > time.monotonic() and signal.has_update()

    def candidate_options(self, title, member_count=None, truncated=False):
        return self.resolver.candidate_options(title, member_count, truncated)

    def resolve_fast(self, title, visible, labels, switched=False, title_changed=False,
                     member_count=None, truncated=False):
        if title_changed:
            self.identity_frame = None
            self.identity_evidence = ([], [])
        recent_username = self.session_signal.observe(switched)
        match = self.resolver.resolve(title, visible, recent_username, labels,
                                      member_count, truncated)
        if match.confirmed and recent_username and match.username != recent_username:
            return Match(title, reason="消息证据与本次清未读记录冲突")
        return match

    def resolve(self, title, lines, labels, chat_pixels, switched=False, title_changed=False,
                member_count=None, truncated=False):
        if title_changed:
            self.identity_frame = None
            self.identity_evidence = ([], [])
        recent_username = self.session_signal.observe(switched)
        text_lines = [line for line in lines if line[0] in ("me", "her")]
        visible = [text for _, _, text, _ in text_lines]
        match = self.resolver.resolve(title, visible, recent_username, labels,
                                      member_count, truncated)
        if (match.ambiguous
                and (len(text_lines) < 3 or not labels)):
            digest = hashlib.blake2b(chat_pixels.tobytes(), digest_size=16).digest()
            if digest != self.identity_frame:
                self.identity_frame = digest
                self.identity_evidence = read_identity_evidence(chat_pixels)
            texts, extra_labels = self.identity_evidence
            if texts:
                candidate = self.resolver.resolve(title, texts, recent_username,
                                                  labels + extra_labels, member_count, truncated)
                if candidate.confirmed:
                    match = candidate
        if match.confirmed and recent_username and match.username != recent_username:
            return Match(title, reason="消息证据与本次清未读记录冲突")
        return match
