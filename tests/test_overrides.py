from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from core import overrides


class OverrideTests(unittest.TestCase):
    def test_forced_event_flag_is_loaded_and_makes_override_nonempty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "override.json"
            path.write_text(json.dumps({"强制事件驱动": True}), encoding="utf-8")
            value = overrides.load(path)
        self.assertTrue(value.ok, value.errors)
        self.assertTrue(value.强制事件驱动)
        self.assertFalse(value.为空)

    def test_forced_event_flag_rejects_non_boolean_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "override.json"
            path.write_text(json.dumps({"强制事件驱动": "是"}), encoding="utf-8")
            value = overrides.load(path)
        self.assertFalse(value.ok)
        self.assertIn("必须是 true 或 false", "；".join(value.errors))


if __name__ == "__main__":
    unittest.main()
