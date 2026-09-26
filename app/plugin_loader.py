"""Load controlled, versioned plugins from folders outside the frozen executable."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

API_VERSION = 1


def plugin_root():
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    return base / "plugins"


def load_plugin(plugin_id, capability, root=None):
    if not re.fullmatch(r"[a-z][a-z0-9_]*", plugin_id):
        raise ValueError("插件 ID 无效")
    directory = (Path(root) if root is not None else plugin_root()) / plugin_id
    try:
        manifest = json.loads((directory / "plugin.json").read_text(encoding="utf-8"))
        if (manifest["id"] != plugin_id or manifest["api_version"] != API_VERSION
                or manifest["capability"] != capability or not manifest["version"]):
            raise ValueError("插件清单与宿主接口不兼容")
        entry = manifest["entry"]
        if not isinstance(entry, str) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*\.py", entry):
            raise ValueError("插件入口无效")
        path = (directory / entry).resolve()
        if not path.is_file() or path.parent != directory.resolve():
            raise ValueError("插件入口不存在")
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("插件清单缺失或无效") from exc
    module_name = "_jev_plugin_" + hashlib.sha256(str(path).casefold().encode()).hexdigest()[:16]
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError("无法加载插件入口")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        if capability == "recognizer" and not all(
                hasattr(module, name) for name in (
                    "read_header", "Match", "Recognition")):
            raise ValueError("识别插件缺少必需接口")
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module
