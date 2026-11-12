"""Reproduce local HTTP fixture receipts. No physical devices are contacted."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

from device_contract import DeviceContractClient, DeviceContractServer, EVIDENCE_KIND


def run():
    payload = {"command_id": "receipt-demo-001", "expected_state_version": 0, "actions": [
        {"name": "light_control", "params": {"location": "客厅", "brightness": 60}}
    ]}
    with DeviceContractServer() as server:
        client = DeviceContractClient(server.url)
        before = server.store.snapshot()
        applied = client.submit(payload)
        duplicate = client.submit(payload)
        conflicting = deepcopy(payload)
        conflicting["actions"][0]["params"]["brightness"] = 20
        conflict = client.submit(conflicting)
        invalid = {"command_id": "receipt-demo-invalid", "expected_state_version": 1,
                   "actions": [conflicting["actions"][0], {"name": "disable_alarm", "params": {}}]}
        rejected = client.submit(invalid)
        queried = client.lookup(payload["command_id"], expected_payload=payload)
        after = server.store.snapshot()
        normal = {"endpoint": server.url, "before": before, "requests": {
            "applied": payload, "duplicate": payload, "conflict": conflicting, "invalid_batch": invalid},
            "results": {"applied": applied, "duplicate": duplicate, "conflict": conflict,
                        "invalid_batch": rejected, "query": queried},
            "http_trace": deepcopy(server.trace), "after": after}
        assert applied["outcome"] == "applied"
        assert applied["receipt"] == duplicate["receipt"]
        assert conflict["http_status"] == 409
        assert rejected["http_status"] == 422
        assert after["applied_commands"] == 1 and after["state_version"] == 1
        assert after["state"]["lights"]["客厅"]["brightness"] == 60
    with DeviceContractServer(response_delay_s=0.15) as server:
        result = DeviceContractClient(server.url, timeout_s=0.03).submit(payload)
        timeout = {"endpoint": server.url, "request": payload, "result": result,
                   "http_trace": deepcopy(server.trace), "after": server.store.snapshot(),
                   "fault": "software_fixture_delays_post_ack_150ms_after_commit"}
        assert result["outcome"] == "applied" and result["post_attempts"] == 1
        assert result["query_attempts"] == 1 and result["post_error"] == "TimeoutError"
        assert [method for method, path in server.trace].count("POST") == 1
    return {"schema": "sensehome.device-contract-fixture-demo.v1", "evidence_kind": EVIDENCE_KIND,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "physical_device_count": 0, "matter_stack_present": False,
            "normal_http": normal, "post_timeout_get_confirmation": timeout,
            "invariants_passed": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        parser.error("Output exists; choose a fresh path to preserve prior evidence")
    result = run()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(args.out.resolve()), "invariants_passed": True,
                      "evidence_kind": EVIDENCE_KIND}, ensure_ascii=False))
