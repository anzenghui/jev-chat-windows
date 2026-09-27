"""Read the fixed local ahucli test customer without linking it to WeChat."""
import json
from datetime import datetime
from pathlib import Path
import re
import shutil
import subprocess


DEFAULT_QUERY = "客户关注哪些课程？"
LIMIT = 10
_REF = re.compile(r"[A-Za-z0-9._-]{20,256}\Z")


def config_path():
    return Path(__file__).resolve().parents[2] / "data" / "ahu_profile_cli.json"


def _text(value, limit=500):
    return value.strip()[:limit] if isinstance(value, str) else ""


def _list_of_dicts(value):
    return value[:8] if isinstance(value, list) else []


def _memory(item):
    content = item.get("content")
    if item.get("memoryType") != "crm_key_event" or not isinstance(content, dict):
        return None
    payload = content.get("payloadJson")
    payload = payload if isinstance(payload, dict) else {}
    statement = _text(payload.get("statedValue"), 500)
    question = _text(payload.get("questionText"), 300)
    response = _text(payload.get("responseText"), 400)
    detail = statement or "\n".join(part for part in (
        f"问：{question}" if question else "", f"答：{response}" if response else "") if part)
    if not detail:
        return None
    quotes = [_text(evidence.get("quote"), 160)
              for evidence in _list_of_dicts(content.get("evidenceJson"))[:3]
              if isinstance(evidence, dict)]
    try:
        when = datetime.fromtimestamp(int(item.get("time", 0)) / 1000).strftime("%m-%d %H:%M")
    except (OSError, OverflowError, TypeError, ValueError):
        when = ""
    return {"title": _text(content.get("eventType"), 80) or "客户记忆",
            "role": _text(content.get("actorRole"), 40), "time": when,
            "detail": detail, "evidence": [quote for quote in quotes if quote]}


def parse_response(value):
    if not isinstance(value, dict) or value.get("code") != 0:
        raise ValueError("画像查询没有成功")
    data = value.get("data")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("画像查询结果格式无效")
    profiles = [item for item in items if isinstance(item, dict)
                and item.get("memoryType") == "ahu_crm_profile"
                and isinstance(item.get("content"), dict)]
    memories = [_memory(item) for item in items if isinstance(item, dict)]
    memories = [item for item in memories if item]
    selected = max(profiles, key=lambda item: (item["content"].get("scopeKey") == "current",
                                               str(item.get("time") or ""))) if profiles else None
    content = selected["content"] if selected else {}
    suggestions = []
    for item in _list_of_dicts(content.get("followupSuggestions")):
        if isinstance(item, dict) and _text(item.get("action")):
            suggestions.append({"action": _text(item.get("action"), 240),
                                "reason": _text(item.get("reason"), 300),
                                "kind": _text(item.get("kind"), 40)})
    emotions = []
    recent = content.get("recentEmotion")
    for item in _list_of_dicts(recent.get("items") if isinstance(recent, dict) else None):
        if isinstance(item, dict) and _text(item.get("emotionText")):
            emotions.append(_text(item.get("emotionText"), 160))
    stages = []
    for item in _list_of_dicts(content.get("sopStages")):
        if isinstance(item, dict) and _text(item.get("stage")):
            stages.append({"stage": _text(item.get("stage"), 50),
                           "status": _text(item.get("status"), 40)})
    evidence = []
    for item in _list_of_dicts(content.get("fieldEvidence"))[:4]:
        if isinstance(item, dict) and _text(item.get("quote")):
            evidence.append(_text(item.get("quote"), 180))
    policy = content.get("contactPolicy")
    return {
        "has_profile": bool(profiles),
        "name": _text(content.get("customerName"), 80),
        "grade": _text(content.get("currentGrade"), 80),
        "grade_reason": _text(content.get("gradeReason"), 400),
        "summary": _text(content.get("communicationSummary"), 1200),
        "study": _text(content.get("studyBasics"), 600),
        "suggestions": suggestions,
        "emotions": emotions,
        "stages": stages,
        "evidence": evidence,
        "contact_policy": {"status": _text(policy.get("status"), 80),
                           "reason": _text(policy.get("reason"), 300)} if isinstance(policy, dict) else {},
        "memories": memories,
    }


def fetch_profile(config=None, runner=subprocess.run, cli=None, query=None):
    chosen_query = DEFAULT_QUERY if query is None else query.strip() if isinstance(query, str) else ""
    if not chosen_query or len(chosen_query) > 500:
        raise ValueError("查询问题不能为空且不能超过 500 字")
    path = Path(config) if config is not None else config_path()
    try:
        customer_ref = json.loads(path.read_text(encoding="utf-8")).get("customer_ref")
    except (OSError, ValueError, AttributeError) as exc:
        raise ValueError("未配置固定测试客户参数") from exc
    if not isinstance(customer_ref, str) or not _REF.fullmatch(customer_ref):
        raise ValueError("固定测试客户参数无效")
    binary = cli or shutil.which("ahucli.cmd") or shutil.which("ahucli")
    if not binary:
        raise ValueError("未找到 ahucli，请先安装或加入 PATH")
    args = [binary, "memory", "search", "--customer-ref", customer_ref,
            "--query", chosen_query, "--limit", str(LIMIT), "--json"]
    try:
        completed = runner(args, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("ahucli 查询失败或超时") from exc
    if completed.returncode != 0:
        raise ValueError("ahucli 查询失败，请检查登录状态")
    if len(completed.stdout) > 2_000_000:
        raise ValueError("画像查询结果过大")
    try:
        result = parse_response(json.loads(completed.stdout))
        result["query"] = chosen_query
        return result
    except json.JSONDecodeError as exc:
        raise ValueError("ahucli 没有返回有效 JSON") from exc
