import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from experiment_inputs import load_json  # noqa: E402
from offload_gateway import BackendResult, LinkSnapshot, OffloadGateway  # noqa: E402
from resource_policy import ResourcePolicyClient  # noqa: E402


class ObservationPolicy:
    def __init__(self):
        self.seen = []

    def decide_observation(self, task_class, observation):
        self.seen.append((task_class, observation))
        return "cloud-model", "COST_CLOUD", "server observation selected cloud"

    def decide(self, task_class, link):
        if task_class == "simple":
            return "local-rules", "TASK_IS_SIMPLE", "simple task"
        if task_class == "safety":
            return "local-rules", "TASK_IS_SAFETY", "safety task"
        raise AssertionError("dual resource policy must receive the observation")


class RecordingBackend:
    def infer(self, command, context=None):
        return BackendResult(True, True, {"answer": "ok"}, [], source="test")


class ResourceObservationTests(unittest.TestCase):
    def test_missing_server_observation_preserves_current_compute_update(self):
        cli = os.path.join(HERE, "policy_cli.exe" if os.name == "nt" else "policy_cli")
        policy = ResourcePolicyClient(
            cli, load_json(os.path.join(HERE, "experiments", "policy_config.json")))
        try:
            gateway = OffloadGateway(
                policy=policy,
                backends={name: RecordingBackend() for name in
                          ("cloud-model", "local-model", "local-rules")},
                link_snapshot=LinkSnapshot(wan_ok=True, wan_rtt_ms=25, lan_ok=True, lan_rtt_ms=0),
            )
            event = gateway.handle("complex request", context={"compute": {
                "model_ready": False, "queue_ms": None, "free_mem_mb": None, "inflight": None,
            }})
        finally:
            policy.close()

        self.assertEqual(event["selected_backend"], "local-model")
        self.assertEqual(event["decision_reason"], "MODEL_UNAVAILABLE")

    def test_complex_decision_uses_and_records_server_owned_observation(self):
        policy = ObservationPolicy()
        observation = {
            "observation_id": 7,
            "observed_age_ms": 15,
            "estimate_scope": "application health; bandwidth unknown",
        }
        gateway = OffloadGateway(
            policy=policy,
            backends={
                "cloud-model": RecordingBackend(),
                "local-model": RecordingBackend(),
                "local-rules": RecordingBackend(),
            },
            link_snapshot=LinkSnapshot(wan_ok=True, wan_rtt_ms=25, lan_ok=True, lan_rtt_ms=0),
        )

        event = gateway.handle("有点冷", observation=observation)

        self.assertEqual(policy.seen, [("complex", observation)])
        self.assertEqual(event["backend"], "cloud-model")
        self.assertEqual(event["reason"], "COST_CLOUD")
        self.assertEqual(event["observation"], observation)

    def test_local_tasks_do_not_consume_dual_resource_observations(self):
        policy = ObservationPolicy()
        gateway = OffloadGateway(
            policy=policy,
            backends={
                "cloud-model": RecordingBackend(),
                "local-model": RecordingBackend(),
                "local-rules": RecordingBackend(),
            },
            link_snapshot=LinkSnapshot(wan_ok=False, lan_ok=True, lan_rtt_ms=0),
        )

        event = gateway.handle("打开客厅的灯", observation={"observation_id": 8})

        self.assertEqual(policy.seen, [])
        self.assertEqual(event["backend"], "local-rules")
        self.assertEqual(event["reason"], "TASK_IS_SIMPLE")


if __name__ == "__main__":
    unittest.main()
