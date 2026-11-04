import json
import os
import tempfile
import threading
import unittest

from offload_gateway import append_jsonl  # noqa: E402


class EventSinkTests(unittest.TestCase):
    def test_jsonl_sink_keeps_one_complete_line_per_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "events.jsonl")
            sink = append_jsonl(path)
            threads = [threading.Thread(target=sink, args=({"event_id": str(i), "ok": True},)) for i in range(50)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            with open(path, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
            self.assertEqual(len(lines), 50)
            events = [json.loads(line) for line in lines]
            self.assertEqual({event["event_id"] for event in events}, {str(i) for i in range(50)})


if __name__ == "__main__":
    unittest.main()
