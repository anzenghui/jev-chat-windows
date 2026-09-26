# -*- coding: utf-8 -*-
"""子进程：截图 → 定位消息区 → OCR 头部会话名和消息 → 按会话去重，全在这边跑。
一次 OCR 250~800ms，放父进程的 Qt 主线程界面就僵了。
只往队列里丢纯 tuple/str（底色 bg 是 numpy，留在这边不过队列）。帧全程内存，绝不落盘。"""
import ctypes
import hashlib
import queue
import time
import traceback

import numpy as np

from app.capture import Capture, chat_area, unminimize
from app.accounts import AccountProbe, snapshot_for_account
from app.ocr import Reader
from app.plugin_loader import load_plugin


def _err(q):
    """异常压成一行发给父进程，子进程的 stderr 一般没人看得见。"""
    q.put(("status", " ".join(traceback.format_exc().split())[-200:]))


def window_context_stamp(context):
    selection = getattr(context, "selection_token", "")
    if selection:
        return "row:" + selection
    return "view:" + hashlib.blake2b(
        repr((context.header.title, context.visible, context.labels)).encode(), digest_size=12).hexdigest()


def _packet(full, area, title, reader, lines):
    """调试视图的一帧：整帧缩到长边 ≤1100 再走队列（原帧 2560 宽裸传要 20MB），只在内存里传，不落盘。
    整数步长切片够用，不引新依赖；框和坐标照发原始值，画的那边按 scale 折算。"""
    k = max(1, -(-max(full.shape[:2]) // 1100))
    small = np.ascontiguousarray(full[::k, ::k])
    return {"w": small.shape[1], "h": small.shape[0], "rgb": small.tobytes(), "scale": k,
            "area": tuple(int(v) for v in area[:4]) if area else None,
            "pane_top": int(area[5]) if area else 0, "title": title,
            "boxes": reader.last_boxes if reader else [],
            "lines": [(w, n, t) for w, n, t, _ in lines],
            "ocr_ms": reader.last_ms if reader else 0, "ts": time.time()}


def run(q, hwnd, enabled, debug_on, commands=None):
    """enabled 置位=采集，清掉=暂停。暂停时停掉 WGC 会话（Windows 那圈黄色采集边框也跟着没了），
    恢复时重开一个；readers 一直留着，去重状态不丢，恢复后不会把屏幕上的旧消息再报一遍。
    debug_on 置位才往队列里送整帧（一帧 2~3MB），关着一点额外活都不干。"""
    try:
        recognizer = load_plugin("session_recognition", "recognizer")
    except Exception as exc:
        q.put(("dead", "会话识别插件无法加载：" + str(exc)[:120]))
        return
    ctypes.windll.user32.SetProcessDPIAware()
    cap = None
    readers = {}  # {识别出的会话键: Reader}，一个会话一套去重状态
    ocr_readers = {}
    title, member_count, truncated, head, current_key = "", None, False, None, ""
    previous_visible, epoch = set(), 0
    account_root = snapshot = None
    account_checked = False
    account_probe = AccountProbe(hwnd)
    processed_account_revision = -1
    recognition = recognizer.Recognition()
    context_reader = getattr(recognizer, "read_window_context", None)
    context, fast_match = None, None
    next_identity_poll, pending_title_change = 0.0, False
    next_fast_refresh = 0.0
    pending_account_refresh = False
    fast_evidence = None
    manual_choice, context_stamp, candidate_marker = None, "", None
    last_full = None
    last_area = None  # 上次发给父进程的 4 元组，变了才再发一次
    warned = False  # 消息区识别失败是否已经报过，拖窗口时别每帧刷一条
    while True:
        if not enabled.is_set():
            if cap is not None:
                cap.stop()
                cap = None
                current_key, head, member_count, truncated = "", None, None, False
                last_full = None
                recognition = recognizer.Recognition(snapshot or "")
                account_probe.reset()
                account_root = snapshot = None
                account_checked = False
                context, fast_match, fast_evidence = None, None, None
                manual_choice, context_stamp, candidate_marker = None, "", None
                pending_account_refresh = False
                next_identity_poll, next_fast_refresh, pending_title_change = 0.0, 0.0, False
                q.put(("paused",))
            enabled.wait()
            continue
        if cap is None:
            try:
                cap = Capture(hwnd)
            except Exception as e:
                q.put(("dead", "无法开始采集：" + (" ".join(str(e).split())[:120] or type(e).__name__)))
                enabled.clear()  # 自己清掉，下一圈就去等着，别一秒重试几十次
                continue
            q.put(("resumed",))
        if not cap.alive():
            break
        try:
            unminimize(hwnd)
            selected_account = account_probe.poll()
            choice_request = None
            if commands is not None:
                while True:
                    try:
                        choice_request = commands.get_nowait()
                    except queue.Empty:
                        break
                if choice_request is not None:
                    next_identity_poll = 0.0
                    if choice_request[0] == "revoke":
                        if choice_request[1] == hwnd and manual_choice:
                            manual_choice = None
                            epoch += 1
                            current_key = f"{title or '当前会话'} [未确认 {hwnd}:{epoch}]"
                            q.put(("chat", current_key, False, "手动选择需要重新确认", "", ""))
                            candidate_marker = None
                        choice_request = None
            now = time.monotonic()
            if now >= next_identity_poll or account_probe.revision != processed_account_revision:
                next_identity_poll = now + 0.2
                selected_snapshot = snapshot_for_account(selected_account)
                if not account_checked or selected_account != account_root or selected_snapshot != snapshot:
                    account_checked = True
                    account_root, snapshot = selected_account, selected_snapshot
                    recognition = recognizer.Recognition(snapshot or "")
                    ocr_readers.clear()
                    context, fast_match, fast_evidence = None, None, None
                    manual_choice, context_stamp, candidate_marker = None, "", None
                    next_fast_refresh = 0.0
                    pending_account_refresh = True
                    previous_visible = set()
                    epoch += 1
                    q.put(("account", account_root.parent.name if account_root else "", bool(snapshot),
                           str(account_root) if account_root else "", str(snapshot) if snapshot else ""))
                    current_key = f"{title or '当前会话'} [未确认 {hwnd}:{epoch}]"
                    q.put(("chat", current_key, False, "正在确认当前微信账号", "", ""))
                    q.put(("candidates", hwnd, "", "", "", "", []))
                processed_account_revision = account_probe.revision
                if context_reader:
                    context = context_reader(hwnd)
                    if context.header.title:
                        name = context.header.title
                        selection = getattr(context, "selection_token", "")
                        stamp = window_context_stamp(context)
                        selection_changed = bool(selection and context_stamp.startswith("row:")
                                                 and stamp != context_stamp)
                        if stamp != context_stamp:
                            manual_choice, context_stamp = None, stamp
                        changed = name != title
                        if changed or selection_changed:
                            title, head, previous_visible = name, None, set()
                            epoch += 1
                            pending_title_change = True
                        member_count, truncated = context.header.member_count, context.header.truncated
                        evidence = (snapshot, stamp, title, context.visible, context.labels,
                                    member_count, truncated)
                        if (evidence != fast_evidence or now >= next_fast_refresh
                                or recognition.needs_refresh()):
                            fast_evidence = evidence
                            next_fast_refresh = now + 1.0
                            fast_match = (recognition.resolve_fast(
                                title, context.visible, context.labels, switched=changed or selection_changed,
                                title_changed=changed or selection_changed,
                                member_count=member_count, truncated=truncated)
                                if snapshot and hasattr(recognition, "resolve_fast") else None)
                        options = (recognition.candidate_options(title, member_count, truncated)
                                   if snapshot and hasattr(recognition, "candidate_options") else [])
                        allowed = {user for user, _ in options}
                        if manual_choice and manual_choice[1] not in allowed:
                            manual_choice = None
                        if choice_request is not None and choice_request[0] == "confirm":
                            _, requested_hwnd, root, source, requested_title, requested_stamp, username = choice_request
                            if (requested_hwnd == hwnd and root == str(account_root)
                                    and source == str(snapshot) and requested_title == title
                                    and requested_stamp == stamp and username in allowed):
                                manual_choice = (stamp, username)
                            else:
                                q.put(("status", "会话已变化，请重新核对并选择身份"))
                        reason = ("无法确定此窗口对应的微信账号" if not account_root
                                  else "已识别微信账号，但没有该账号的明文快照")
                        match = fast_match or recognizer.Match(title, reason=reason)
                        if manual_choice:
                            if match.confirmed and match.username != manual_choice[1]:
                                manual_choice = None
                                match = recognizer.Match(title, reason="手动选择与消息证据冲突")
                            else:
                                match = recognizer.Match(title, manual_choice[1])
                        marker = (title, str(account_root), str(snapshot), stamp,
                                  tuple(options) if not match.confirmed else ())
                        account = hashlib.sha256(str(account_root).casefold().encode()).hexdigest()[:8]
                        key = (f"{title} [{account}:{match.username}]" if match.confirmed
                               else f"{title} [未确认 {hwnd}:{epoch}]")
                        if key != current_key:
                            if "[未确认 " in current_key:
                                readers.pop(current_key, None)
                            current_key = key
                            q.put(("chat", key, match.confirmed, match.reason,
                                   str(snapshot) if match.confirmed else "", match.username,
                                   stamp if manual_choice and match.confirmed else ""))
                        if marker != candidate_marker:
                            candidate_marker = marker
                            q.put(("candidates", hwnd, *marker[:4], list(marker[4])))
                    elif manual_choice:
                        manual_choice = None
                        epoch += 1
                        current_key = f"{title or '当前会话'} [未确认 {hwnd}:{epoch}]"
                        q.put(("chat", current_key, False, "无法继续验证手动选择的会话", "", ""))
                        q.put(("candidates", hwnd, "", "", "", "", []))
            full = cap.settled()
            if (full is None and last_full is not None
                    and (pending_account_refresh or recognition.needs_refresh())):
                full = last_full
            if full is not None:
                pending_account_refresh = False
                last_full = full
                reader, lines = None, []  # 调试视图要用，消息区没认出来时就是空的
                area = chat_area(full)  # 每次停稳都重算：拖完窗口微信布局会晚一拍才铺好，只按尺寸变化算一次会锁死
                if area is None:
                    if not warned:
                        q.put(("status", "消息区认不出来（窗口太小？）"))
                        warned = True
                else:
                    warned = False
                    cap.area = area  # 采集线程拿它做 diff
                    x0, y0, x1, y1, bg, y_pane = area
                    rect = (x0, y0, x1, y1)
                    if rect != last_area:
                        q.put(("area", rect))
                        last_area = rect
                    crop = full[y_pane:y0, x0:x1]  # 头部：会话名在这里
                    title_changed = pending_title_change
                    pending_title_change = False
                    if head is None or not np.array_equal(crop, head):  # 名字没动就别白跑一次 OCR
                        head = crop
                        header = context.header if context else getattr(recognizer, "read_window_header", lambda _: None)(hwnd)
                        if not header or not header.title:
                            header = recognizer.read_header(crop)
                        name, member_count, truncated = header.title, header.member_count, header.truncated
                        if name != title:
                            title, previous_visible = name, set()
                            manual_choice, context_stamp = None, ""
                            q.put(("candidates", hwnd, "", "", "", "", []))
                            epoch += 1
                            title_changed = True
                    chat_pixels = full[y0:y1, x0:x1]
                    fast_probed = bool(snapshot and context and context.header.title == title
                                       and hasattr(recognition, "resolve_fast"))
                    ocr_reader = ocr_readers.setdefault(title, Reader())
                    lines = ocr_reader.read(chat_pixels, bg)
                    visible = {(who, text) for who, _, text, _ in lines if who in ("me", "her")}
                    view_changed = (bool(visible and previous_visible)
                                    and not visible.intersection(previous_visible))
                    if not title_changed and view_changed:
                        epoch += 1
                    labels = [box[5] for box in ocr_reader.last_boxes if box[4] == "gray"]
                    if fast_match and fast_match.confirmed:
                        recent = recognition.session_signal.observe(view_changed) if view_changed else ""
                        match = (fast_match if not recent or recent == fast_match.username else
                                 recognizer.Match(title, reason="UIA 消息与本次清未读记录冲突"))
                    else:
                        match = recognition.resolve(title, lines, labels, chat_pixels,
                                                    switched=view_changed or (title_changed and not fast_probed),
                                                    title_changed=title_changed,
                                                    member_count=member_count, truncated=truncated)
                    if not snapshot:
                        reason = ("无法确定此窗口对应的微信账号" if not account_root
                                  else "已识别微信账号，但没有该账号的明文快照")
                        match = recognizer.Match(title, reason=reason)
                    if manual_choice and context and context.header.title == title:
                        if match.confirmed and match.username != manual_choice[1]:
                            manual_choice = None
                            match = recognizer.Match(title, reason="手动选择与消息证据冲突")
                        else:
                            match = recognizer.Match(title, manual_choice[1])
                    previous_visible = visible
                    account = hashlib.sha256(str(account_root).casefold().encode()).hexdigest()[:8]
                    key = (f"{title} [{account}:{match.username}]" if match.confirmed
                           else f"{title or '当前会话'} [未确认 {hwnd}:{epoch}]")
                    if key != current_key:
                        if "[未确认 " in current_key:
                            readers.pop(current_key, None)
                        current_key = key
                        q.put(("chat", key, match.confirmed, match.reason,
                               str(snapshot) if match.confirmed else "", match.username,
                               context_stamp if manual_choice and match.confirmed else ""))
                    first_view = key not in readers
                    reader = readers.setdefault(key, Reader())
                    new = reader.new_lines(lines)
                    if new:
                        q.put(("baseline" if first_view else "lines", key, new, rect))
                if debug_on.is_set():
                    q.put(("debug", _packet(full, area, title, reader, lines)))
        except Exception:
            _err(q)  # 一帧出错不退出
        time.sleep(0.05)
    q.put(("dead", "采集停了（聊天窗口关了？）"))
    try:
        cap.wait()  # 采集线程若是报错死的，这里把错抛出来
    except Exception:
        _err(q)
