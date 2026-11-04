import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import bridge_server  # noqa: E402


class HttpBoundaryTests(unittest.TestCase):
    def test_command_payload_requires_nonempty_string_and_preserves_context(self):
        with self.assertRaisesRegex(ValueError, "INVALID_COMMAND"):
            bridge_server.parse_command_payload({"command": ""})
        with self.assertRaisesRegex(ValueError, "INVALID_COMMAND"):
            bridge_server.parse_command_payload({"command": 3})
        payload = bridge_server.parse_command_payload(
            {"command": "打开客厅的灯", "context": {"session": "demo"}}
        )
        self.assertEqual(payload["command"], "打开客厅的灯")
        self.assertEqual(payload["context"], {"session": "demo"})

    def test_command_payload_rejects_oversized_body(self):
        with self.assertRaisesRegex(ValueError, "REQUEST_TOO_LARGE"):
            bridge_server.parse_json_request(b"x" * (bridge_server.MAX_REQUEST_BODY_BYTES + 1))

    def test_light_request_uses_authoritative_action_executor(self):
        original_file = bridge_server.STATE_FILE
        try:
            with tempfile.TemporaryDirectory() as folder:
                bridge_server.STATE_FILE = str(Path(folder) / "device_state.json")
                bridge_server.save_state(bridge_server.default_state())
                result = bridge_server.apply_light_request(
                    {"location": "客厅", "action": "set", "brightness": 55}
                )
                self.assertTrue(result["semantic_complete"])
                self.assertEqual(bridge_server.load_state()["lights"]["客厅"]["brightness"], 55)
                with self.assertRaisesRegex(ValueError, "INVALID_LOCATION"):
                    bridge_server.apply_light_request(
                        {"location": "不存在", "action": "on"}
                    )
                self.assertNotIn("不存在", bridge_server.load_state()["lights"])
        finally:
            bridge_server.STATE_FILE = original_file

    def test_balcony_is_not_aliased_to_outdoor(self):
        self.assertIsNone(bridge_server.extract_location("把阳台灯打开"))
        self.assertEqual(bridge_server.extract_location("把室外灯打开"), "室外")


if __name__ == "__main__":
    unittest.main()
