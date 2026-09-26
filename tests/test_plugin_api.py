from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.plugin_api import open_snapshot


class PluginApiTests(unittest.TestCase):
    def test_never_opens_wechat_source_database(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "db_storage" / "contact" / "contact.db"
            with patch("app.plugin_api.sqlite3.connect") as connect:
                with self.assertRaisesRegex(ValueError, "原始数据库"):
                    with open_snapshot(source):
                        pass
                connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
