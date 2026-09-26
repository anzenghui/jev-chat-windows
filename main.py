# -*- coding: utf-8 -*-
"""父进程：只管界面。截图 + OCR 在 app/worker.py 的子进程里跑，队列里收新消息 →
冒出新的对方消息才调 engine → 悬浮窗给 3 条候选 → 人点「填入」。发送永远手动。静默期零调用。
上下文、结果、聊天记录都按会话名（子进程 OCR 头部标题得来）分开存，切会话不串味。

    pip install rapidocr-onnxruntime numpy windows-capture PySide6-Fluent-Widgets
两个模型（判断 Jev / 起草语言模型）的来源和 key 在独立设置页填写，不用改代码。IDE 里直接 Run。
"""
import ctypes
import multiprocessing
from pathlib import Path
import queue
import threading
import time
import traceback
from collections import deque

from app import settings, update, worker
from app.accounts import account_for_window
from app.capture import find_wechat_hwnd
from app.fill import fill
from app.overlay import Overlay
from app.plugin_loader import load_plugin
from app.version import VERSION
from core.engine import analyze

# {会话名: {history, result, rev, target, senders}}：每个会话各自的上下文、上次结果和版本号，互不串味
# history 里是 [(who, text, name)]，engine 只认 her/me，name 是群里的发言人（单聊/自己说的是 None）；
# 只是缓冲区，实际喂模型几条由设置里的「参考上下文」决定
# senders：这个群里发过言的人，去重、最近的排最前；target：用户挑的回复对象（None = 跟着最近那个走）
chats = {}
state = {"area": None, "busy": False, "rerun": None, "hwnd": None, "chat": "", "confirmed": False}
results = queue.Queue()
update_result = queue.Queue()  # 独立小队列，别跟 results 的 (kind, r, title, revision) 形状搅在一起
prepare_result = queue.Queue()
prepare_lock = threading.Lock()
history_result = queue.Queue()
history_sources = {}
history_state = {}
sync_result = queue.Queue()
sync_lock = threading.Lock()
commands = None


def chat_of(title):
    return chats.setdefault(title, {"history": deque(maxlen=60), "result": None, "rev": 0,
                                    "target": None, "senders": []})


def target_of(title):
    """这个会话现在的回复对象：用户挑过且人还在就用它，否则用最近说话的那个；单聊没有发言人 → None。"""
    chat = chat_of(title)
    if chat["target"] in chat["senders"]:
        return chat["target"]
    return chat["senders"][0] if chat["senders"] else None


def fill_reply(text):
    if not capture_on.is_set() or not state["confirmed"] or ov.current_chat() != state["chat"]:
        raise RuntimeError("当前会话尚未确认，不能填入")
    if state["hwnd"] is None:  # 子进程重开过，hwnd 可能换了，用最新的
        raise RuntimeError("未找到聊天窗口，请确认已经打开")
    if state["area"] is None:
        raise RuntimeError("输入区域尚不可用，请确认聊天窗口可见（不要最小化）")
    manual = state.get("manual_context")
    if manual:
        try:
            observation = load_plugin("session_recognition", "recognizer").read_window_context(state["hwnd"])
            same_window = (observation.header.title == manual[0]
                           and worker.window_context_stamp(observation) == manual[1])
            same_account = (account_for_window(state["hwnd"]) == Path(state["account_root"]).resolve())
        except Exception:
            same_window = same_account = False
        if not same_window or not same_account:
            state["confirmed"] = False
            ov.invalidate_replies()
            if commands is not None:
                commands.put(("revoke", state["hwnd"]))
            raise RuntimeError("微信会话或账号已变化，请重新确认身份")
    if settings.reply_target() and ov.at_prefix_enabled():
        target = target_of(ov.current_chat())  # 填进去的是界面上正看着的那个会话的对象
        if target:
            text = f"@{target} " + text  # 纯文本，微信不认成真正的 @，只是让群里看得出在跟谁说
    fill(state["hwnd"], state["area"], text)


def spawn_worker():
    """开一个采集子进程，它跟着 capture_on 走：置位=采集，清掉=暂停。"""
    p = multiprocessing.Process(target=worker.run,
                                args=(q, state["hwnd"], capture_on, debug_on, commands), daemon=True)
    p.start()
    return p


def follow_wechat_window():
    """Only the currently selected WeChat window may supply conversation events."""
    global child, q, commands
    if not capture_on.is_set() or time.monotonic() < state.get("next_window_check", 0):
        return
    state["next_window_check"] = time.monotonic() + 0.35
    try:
        selected = find_wechat_hwnd(state["hwnd"])
    except RuntimeError as exc:
        selected = None
        reason = str(exc)
    if selected is None and state["hwnd"] is None and child is None:
        return
    if selected == state["hwnd"] and child is not None:
        return
    if child is not None:
        child.terminate()
        child.join()
        child = None
    old_q, q = q, multiprocessing.Queue()
    old_q.close()
    if commands is not None:
        commands.close()
    commands = multiprocessing.Queue()
    for chat in chats.values():
        chat["rev"] += 1
    state.update(hwnd=selected, area=None, chat="", confirmed=False, rerun=None, busy=False,
                 account_root="", snapshot="", candidate_context=None, manual_context=None)
    ov.set_busy(False)
    ov.invalidate_replies()
    ov.set_account_status("", False)
    ov.set_identity_candidates(None, [])
    if selected is None:
        ov.set_status(reason, "warning")
        return
    child = spawn_worker()


def set_debug(on):
    """调试视图开关：开 → 开窗 + 置位（子进程这才开始送帧，一帧 2~3MB）；关 → 清掉 + 收窗。"""
    global dbg
    if not on:
        debug_on.clear()
        if dbg is not None:
            dbg.hide()
        return
    if dbg is None:
        from app.debugwin import DebugWindow

        dbg = DebugWindow(on_close=on_debug_closed)
    dbg.show()
    debug_on.set()


def on_debug_closed():
    """用户直接关了调试窗 = 把开关也关了，否则设置页显示开着但没窗。"""
    debug_on.clear()
    ov.set_debug_switch(False)
    settings.save(debug_view_on=False)


def on_toggle_capture(on):
    """标题栏开关。启动时没找到微信就没有子进程，这会儿再找一次，找到了才真开得起来。"""
    global child
    if not on:
        capture_on.clear()
        state["confirmed"] = False
        state["manual_context"] = None
        ov.invalidate_replies()
        return
    if child is None:
        try:
            state["hwnd"] = find_wechat_hwnd(state["hwnd"])
        except RuntimeError:
            capture_on.set()  # wait for the user to activate a WeChat window
            ov.set_capture(True)
            ov.set_status("请点一下要使用的微信窗口", "warning")
            return
        child = spawn_worker()
    capture_on.set()


def analyze_bg(msgs, title, revision, reply_to=None):
    """后台线程只跑网络调用，结果丢队列；UI 只在主线程的 tick 里动（Qt 不能跨线程碰）。"""
    try:
        results.put(("ok", analyze(msgs, settings.relationship(), context=settings.context(),
                                   model=settings.draft_model() or None,
                                   provider=settings.draft_provider(),
                                   base_url=settings.draft_base_url() or None,
                                   reply_to=reply_to, style=settings.style(),
                                   thinking=settings.thinking(),
                                   jev_provider=settings.jev_provider(),
                                   jev_model=settings.jev_model() or None),
                     title, revision))
    except Exception as e:
        results.put(("err", f"分析失败: {e}", title, revision))


def check_update_bg():
    """启动时后台查一次新版本，跟 analyze_bg 一个套路：网络调用在线程里，UI 只在 tick() 里动。"""
    r = update.check_latest(VERSION)
    if r:
        update_result.put(r)


def on_prepare_db(key_file=None):
    if state["hwnd"] is None:
        ov.set_status("请先激活要使用的微信窗口", "warning")
        return
    account = account_for_window(state["hwnd"])
    if account is None:
        ov.set_status("无法唯一确定当前微信账号，未准备数据库", "warning")
        return
    if not prepare_lock.acquire(blocking=False):
        return
    ov.set_prepare_busy(True)
    ov.set_status("正在复制并准备当前账号数据库", "busy")

    def work():
        try:
            from app.db_snapshot import prepare_database
            prepare_database(account, key_file)
            prepare_result.put((account, None))
        except ValueError as exc:
            prepare_result.put((account, str(exc)))
        except Exception:
            prepare_result.put((account, "数据库准备失败，请检查空间、文件权限或密钥文件。"))
        finally:
            prepare_lock.release()

    threading.Thread(target=work, daemon=True).start()


def on_load_history(chat):
    source = history_sources.get(chat)
    if not source:
        return
    page = history_state.setdefault(chat, {"cursor": None, "loaded": False, "loading": False, "more": True})
    if page["loading"] or (page["loaded"] and not page["more"]):
        return
    page["loading"] = True
    ov.set_history_loading(chat, True)
    before = page["cursor"]

    def work():
        try:
            from app.history import read_page
            rows, cursor, more = read_page(source[0], source[1], before)
            history_result.put((chat, source, rows, cursor, more, None))
        except Exception:
            history_result.put((chat, source, [], before, False, "读取聊天记录失败"))

    threading.Thread(target=work, daemon=True).start()


def poll_database_sync():
    if time.monotonic() < state.get("next_db_sync", 0):
        return
    state["next_db_sync"] = time.monotonic() + 1.0
    root, snapshot, hwnd = state.get("account_root"), state.get("snapshot"), state["hwnd"]
    username = history_sources.get(state.get("chat"), (None, ""))[1] if state.get("confirmed") else ""
    if (not root or not snapshot or not capture_on.is_set()
            or not sync_lock.acquire(blocking=False)):
        return

    def work():
        try:
            from app.db_incremental import refresh_legacy_contact, refresh_legacy_session, sync_account
            cancelled = lambda: state["hwnd"] != hwnd or state.get("account_root") != root or not capture_on.is_set()
            if (Path(snapshot) / "sync.json").is_file():
                changed, rebased = sync_account(
                    root, snapshot, username=username, cancelled=cancelled,
                    progress=lambda kind: sync_result.put((root, snapshot, [], True, "syncing")) if kind == "rebase" else None)
            else:
                changed = []
                if refresh_legacy_session(root, snapshot, cancelled):
                    changed.append("session/session.db")
                if refresh_legacy_contact(root, snapshot, cancelled):
                    changed.append("contact/contact.db")
                rebased = False
            if changed:
                sync_result.put((root, snapshot, changed, rebased, None))
        except ValueError as exc:
            sync_result.put((root, snapshot, [], False, str(exc)))
        except InterruptedError:
            pass
        except Exception:
            sync_result.put((root, snapshot, [], False, "数据库增量更新失败；旧快照仍可读。"))
        finally:
            sync_lock.release()

    threading.Thread(target=work, daemon=True).start()


def start_analyze(title, msgs):
    if not state["confirmed"] or title != state["chat"]:
        return
    if not settings.has_jev_key():
        ov.set_status("请先在设置中配置模型", "warning")
        return
    if not settings.has_llm_key():
        ov.set_status(f"起草来源 {settings.draft_provider_name()} 没填密钥，去设置里补上", "warning")
        return
    state["busy"] = True
    ov.set_busy(True)
    reply_to = target_of(title) if settings.reply_target() else None  # 开关关着就是今天的行为
    threading.Thread(target=analyze_bg, args=(msgs, title, chat_of(title)["rev"], reply_to),
                     daemon=True).start()


def on_target_change(title, name):
    """用户挑了回复对象：记下来，这个会话里有对方的话就照新对象重跑一次。"""
    chat = chat_of(title)
    chat["target"] = name
    msgs = list(chat["history"])
    if not any(m[0] == "her" for m in msgs):
        return
    if state["busy"]:
        state["rerun"] = (title, msgs)
        ov.set_busy(True)
    else:
        start_analyze(title, msgs)


def on_confirm_identity(context, username):
    if (not capture_on.is_set() or commands is None
            or context != state.get("candidate_context") or context[0] != state.get("hwnd")
            or context[1] != state.get("account_root") or context[2] != state.get("snapshot")):
        ov.set_status("会话已变化，请重新选择身份", "warning")
        return
    commands.put(("confirm", *context, username))
    ov.set_status("正在复核当前会话身份", "busy")


def drain():
    """把子进程队列里攒的东西全收掉。"""
    global child
    while True:
        try:
            msg = q.get_nowait()
        except queue.Empty:
            return
        kind = msg[0]
        if kind == "area":  # 只是窗口挪了位置，坐标跟着更新，别的什么都不用动
            state["area"] = msg[1]
            continue
        if kind == "chat":  # 微信切了会话，界面跟过去（用户正浏览别的会话时也跟，微信是准的）
            previous_chat, was_confirmed = state["chat"], state["confirmed"]
            if state["chat"] != msg[1]:
                ov.invalidate_replies()
            state["chat"] = msg[1]
            state["confirmed"] = msg[2]
            state["manual_context"] = (msg[1].rsplit(" [", 1)[0], msg[6]) if msg[2] and len(msg) > 6 and msg[6] else None
            ov.set_chat(msg[1])
            state["candidate_context"] = None
            ov.set_identity_candidates(None, [])
            source = (msg[4], msg[5]) if msg[2] and len(msg) > 5 else None
            if source and history_sources.get(msg[1]) != source:
                history_sources[msg[1]] = source
                history_state.pop(msg[1], None)
                on_load_history(msg[1])
            if not msg[2]:
                ov.invalidate_replies()
                ov.set_status(msg[3] or "会话身份待确认", "warning")
            elif not was_confirmed or previous_chat != msg[1]:
                ov.set_status("已按你的选择确认，等待新消息" if state["manual_context"] else
                              "会话已确认，等待对方的新消息", "idle")
            continue
        if kind == "candidates":
            if msg[1] == state.get("hwnd"):
                context = (msg[1], msg[3], msg[4], msg[2], msg[5]) if msg[6] else None
                state["candidate_context"] = context
                ov.set_identity_candidates(context, msg[6])
            continue
        if kind == "account":
            ov.set_account_status(msg[1], msg[2])
            state["account_root"] = msg[3] if len(msg) > 3 else ""
            state["snapshot"] = msg[4] if len(msg) > 4 else ""
            continue
        if kind == "debug":  # 调试视图的一帧；窗口不在就直接丢掉
            if dbg is not None:
                dbg.show_packet(msg[1])
            continue
        if kind == "status":  # 单帧识别失败/报错，提示一下就好，别把正在跑的分析和已知坐标清掉
            ov.set_status(msg[1], "warning")
            ov.log(msg[1])
            continue
        if kind == "paused":  # 子进程确认已暂停
            state["confirmed"] = False
            state["manual_context"] = None
            state["candidate_context"] = None
            ov.set_identity_candidates(None, [])
            ov.invalidate_replies()
            ov.set_capture(False)
            continue
        if kind == "resumed":  # 子进程重新开始采集
            ov.set_capture(True)
            continue
        if kind == "dead":  # 采集彻底停了（微信关了之类），这才是真的要清状态
            state["area"] = None
            state["confirmed"] = False
            state["manual_context"] = None
            state["candidate_context"] = None
            ov.set_identity_candidates(None, [])
            for c in chats.values():  # 在跑的分析作废，回来的结果不再往界面上贴
                c["rev"] += 1
            state["rerun"] = None
            ov.invalidate_replies()
            ov.set_busy(False)
            ov.set_capture(False, msg[1])
            ov.log(msg[1])
            if child is not None:  # 子进程已经不干活了，收掉引用，下次打开开关重开一个
                child.terminate()
                child.join()
                child = None
            continue
        _, title, new, area = msg
        baseline = kind == "baseline"
        state["area"] = area
        chat = chat_of(title)
        chat["rev"] += 1  # 这个会话有新消息了，它在跑的分析作废
        if title == ov.current_chat():  # 看的是别的会话就别把人家的候选划掉
            ov.invalidate_replies()
        for who, name, text in new:
            if who in ("her", "me"):
                chat["history"].append((who, text, name))
            ov.log_message(who, text, name, chat=title)
            if who == "her" and name:  # 群里发过言的人，去重后最近的排最前
                if name in chat["senders"]:
                    chat["senders"].remove(name)
                chat["senders"].insert(0, name)
        ov.set_targets(title, chat["senders"], target_of(title))  # 显不显示这一行由悬浮窗按开关决定
        if baseline:
            continue
        actionable = [item for item in new if item[0] in ("her", "me")]
        if actionable and actionable[-1][0] == "her" and state["confirmed"]:  # 图片文字不单独触发模型
            msgs = list(chat["history"])
            if state["busy"]:
                state["rerun"] = (title, msgs)
                ov.set_busy(True)
            else:
                start_analyze(title, msgs)
        elif actionable and actionable[-1][0] == "me":
            state["rerun"] = None
            ov.set_busy(False)
            ov.set_status("你已回复，等待对方的新消息")


def tick():
    global child
    try:
        follow_wechat_window()
        drain()
        poll_database_sync()
        while not sync_result.empty():
            root, snapshot, changed, rebased, error = sync_result.get()
            if root != state.get("account_root") or snapshot != state.get("snapshot"):
                continue
            if error == "syncing":
                ov.set_status("消息分库重同步中，历史记录暂不更新", "busy")
                continue
            if error:
                state["next_db_sync"] = time.monotonic() + 10
                if error != state.get("last_sync_error"):
                    ov.set_status(error, "warning")
                    state["last_sync_error"] = error
                chat = state["chat"]
                page = history_state.get(chat)
                if page and page["loaded"] and not page["loading"]:
                    history_state.pop(chat, None)
                    on_load_history(chat)
                continue
            state["last_sync_error"] = None
            if rebased:
                ov.set_status("消息分库已重同步", "success")
            chat = state["chat"]
            if any(relative.startswith("message/") for relative in changed) and chat in history_sources:
                page = history_state.get(chat)
                if page and page["loading"]:
                    page["refresh_pending"] = True
                elif page and page["loaded"]:
                    history_state.pop(chat, None)
                    on_load_history(chat)
        while not history_result.empty():
            chat, source, rows, cursor, more, error = history_result.get()
            if history_sources.get(chat) != source:
                continue
            page = history_state.get(chat)
            if page is None:
                continue
            page["loading"] = False
            ov.set_history_loading(chat, False)
            if error:
                ov.set_status(error, "warning")
                continue
            first = not page["loaded"]
            page.update(cursor=cursor, loaded=True, more=more)
            ov.set_history_page(chat, rows, more, first_page=first)
            if page.get("refresh_pending"):
                history_state.pop(chat, None)
                on_load_history(chat)
        while not prepare_result.empty():
            account, error = prepare_result.get()
            ov.set_prepare_busy(False)
            if error:
                ov.set_status(error, "error")
            elif state["hwnd"] is not None and account_for_window(state["hwnd"]) == account:
                ov.set_status("数据库副本已就绪，正在重新识别会话", "success")
                state["confirmed"] = False
                state["manual_context"] = None
                state["area"] = None
                state["snapshot"] = ""
                state["rerun"] = None
                state["busy"] = False
                for chat in chats.values():
                    chat["rev"] += 1
                ov.set_busy(False)
                ov.invalidate_replies()
                if child is not None:
                    child.terminate()
                    child.join()
                    child = None
                state["next_window_check"] = 0
            else:
                ov.set_status("数据库副本已就绪；切回对应微信窗口后自动识别", "success")
        while not update_result.empty():
            latest, url = update_result.get()
            ov.set_update(latest, url)
        while not results.empty():
            kind, r, title, revision = results.get()
            state["busy"] = False
            if state["rerun"]:  # 分析期间又来了新消息，接着跑最新的
                (t, msgs), state["rerun"] = state["rerun"], None
                start_analyze(t, msgs)
                continue
            if revision != chat_of(title)["rev"]:  # 这个会话后来又说话了，这份结果过期了
                ov.set_busy(False)
                continue
            if kind == "ok":
                chat_of(title)["result"] = r  # 先存着；正看着这个会话才立刻贴上去
                if title == ov.current_chat() and state["confirmed"] and capture_on.is_set():
                    ov.show(r)
                else:
                    ov.set_busy(False)
            else:
                ov.set_busy(False)
                ov.set_status("生成失败，请检查网络和服务设置；新消息到来后会重试。", "error")
                ov.log(r)
    except Exception:
        traceback.print_exc()  # 一帧出错不退出
    ov.after(50, tick)


if __name__ == "__main__":  # Windows 的 spawn 会让子进程重新执行本文件，没这行就无限套娃开进程
    multiprocessing.freeze_support()  # 打包成 exe 后 spawn 出来的子进程会重跑一遍 exe，没这行就无限弹界面
    ctypes.windll.user32.SetProcessDPIAware()
    q = multiprocessing.Queue()
    commands = multiprocessing.Queue()
    capture_on = multiprocessing.Event()  # 父子进程共用的开关，置位=采集
    debug_on = multiprocessing.Event()  # 同上，置位=子进程往队列里送整帧给调试窗
    ov = Overlay(on_fill=fill_reply, on_toggle_capture=on_toggle_capture,
                 on_target_change=on_target_change, on_toggle_debug=set_debug,
                 on_prepare_db=on_prepare_db, on_load_history=on_load_history,
                 on_confirm_identity=on_confirm_identity,
                 result_of=lambda t: chats.get(t, {}).get("result"))
    child = dbg = None
    capture_on.set()
    try:
        state["hwnd"] = find_wechat_hwnd()
    except RuntimeError:
            ov.set_capture(True)
            ov.set_status("请点一下要使用的微信窗口", "warning")
    else:
        child = spawn_worker()
    if settings.debug_view():  # 上次开着就直接开回来
        set_debug(True)
    if not settings.has_jev_key():
        ov.set_status("请先在设置中配置模型", "warning")
        ov.after(0, ov.open_settings)
    if settings.check_update() and update.parse_version(VERSION):  # 开发版没有版本号，不查也不烦源码用户
        threading.Thread(target=check_update_bg, daemon=True).start()
    ov.after(50, tick)
    try:
        ov.run()
    finally:
        if child is not None:
            child.terminate()
