import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from app.plugin_loader import load_plugin, plugin_root


class PluginLoaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "test_recognizer"
        self.folder.mkdir()
        self.manifest = {
            "id": "test_recognizer", "version": "1.0.0", "api_version": 1,
            "capability": "recognizer", "entry": "plugin.py",
        }

    def write_plugin(self):
        (self.folder / "plugin.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        (self.folder / "plugin.py").write_text(
            "def read_header(image): return 'test'\n"
            "class Match: pass\nclass Recognition: pass\n",
            encoding="utf-8")

    def test_loads_external_plugin_directory(self):
        self.write_plugin()
        plugin = load_plugin("test_recognizer", "recognizer", self.root)
        self.assertEqual(plugin.read_header(None), "test")
        self.assertEqual(Path(plugin.__file__).parent, self.folder)

    def test_rejects_incompatible_manifest_and_path_escape(self):
        self.manifest["api_version"] = 2
        self.write_plugin()
        with self.assertRaisesRegex(ValueError, "不兼容"):
            load_plugin("test_recognizer", "recognizer", self.root)
        self.manifest["api_version"] = 1
        self.manifest["entry"] = "../outside.py"
        self.write_plugin()
        with self.assertRaisesRegex(ValueError, "入口无效"):
            load_plugin("test_recognizer", "recognizer", self.root)

    def test_frozen_app_looks_beside_executable(self):
        with patch.object(sys, "frozen", True, create=True), patch.object(
                sys, "executable", str(self.root / "jev-chat-windows.exe")):
            self.assertEqual(plugin_root(), self.root / "plugins")


if __name__ == "__main__":
    unittest.main()
