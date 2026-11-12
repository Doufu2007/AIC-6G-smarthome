from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

NET = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NET))
STATE = {"lights": {"客厅": {"is_on": False, "brightness": 0}},
         "curtains": {"客厅": {"is_open": False, "position": 0}},
         "temperature": {"客厅": {"temp": 25, "humidity": 50}}}
ACTION = [{"name": "light_control", "params": {"location": "客厅", "brightness": 60}}]


class MQTTReceiptTests(unittest.TestCase):
    def test_real_broker_entity_protocol_persists_state_and_deduplicates(self):
        if importlib.util.find_spec("mqtt_receipts") is None:
            self.fail("missing real loopback MQTT software entity adapter")
        from durable_receipts import SQLiteReceiptExecutor
        from mqtt_receipts import LocalMQTTSession, MQTTReceiptTransport
        with tempfile.TemporaryDirectory() as tmp:
            with LocalMQTTSession(Path(tmp), initial_state=deepcopy(STATE)) as session:
                transport = MQTTReceiptTransport("127.0.0.1", session.port, session.platform_id)
                try:
                    executor = SQLiteReceiptExecutor(Path(tmp) / "gateway.sqlite3", transport, platform_id=session.platform_id)
                    first = executor(ACTION, {"command_id": "mqtt-1", "_receipt_command": "客厅有点暗"})
                    self.assertEqual(first["execution_state"], "applied")
                    repeated = executor(ACTION, {"command_id": "mqtt-1", "_receipt_command": "客厅有点暗"})
                    self.assertEqual(repeated["receipt"]["state_version_after"], 1)
                    self.assertEqual(first["receipt"]["software_state"]["lights"]["客厅"]["brightness"], 60)
                    self.assertFalse(first["physical_execution_confirmed"])
                    session.restart_entity()
                    reported = transport.lookup("mqtt-1")
                    self.assertEqual(reported["state_version_after"], 1)
                    self.assertEqual(reported["software_state"]["lights"]["客厅"]["brightness"], 60)
                finally:
                    transport.close()
            self.assertTrue((Path(tmp) / "cleanup.json").is_file())


if __name__ == "__main__":
    unittest.main()
