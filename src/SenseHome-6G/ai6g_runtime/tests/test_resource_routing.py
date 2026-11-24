import unittest
import time
from types import SimpleNamespace

from ai6g_runtime.router import HealthObservation, ObservationCache, ResourceObservation, TaskRouter
from ai6g_runtime.runtime import TaskRuntime


def _observation(name, latency_ms, *, checked_at=100.0, quality=0.95, resources=None):
    resource_observation = ResourceObservation(**resources) if resources is not None else None
    return HealthObservation(name, True, latency_ms, quality, checked_at, resources=resource_observation)


def _task(*, privacy="standard"):
    return SimpleNamespace(
        task_class="complex",
        family="general",
        deadline_ms=500,
        quality_required=0.9,
        privacy=privacy,
    )


class ResourceRoutingTests(unittest.TestCase):
    def test_fresh_overloaded_local_backend_is_excluded(self):
        cache = ObservationCache(ttl_s=30)
        cache.put(_observation("local-model", 40, resources={
            "queue_ms": 350, "free_mem_mb": 2048, "inflight": 1,
            "source": "node-exporter", "observed_at": 100.0,
        }))
        cache.put(_observation("cloud-model", 100))

        decision = TaskRouter(cache).choose(_task(), now=101.0)

        self.assertEqual(decision.backend, "cloud-model")
        self.assertIn("RESOURCE", decision.reason)

    def test_stale_resource_fields_do_not_override_fresh_health_latency(self):
        cache = ObservationCache(ttl_s=30)
        cache.put(_observation("local-model", 40, resources={
            "queue_ms": 350, "free_mem_mb": 100, "inflight": 20,
            "source": "node-exporter", "observed_at": 90.0,
        }))
        cache.put(_observation("cloud-model", 100))

        decision = TaskRouter(cache).choose(_task(), now=101.0)

        self.assertEqual(decision.backend, "local-model")
        self.assertFalse(decision.resource_observation["used"])
        self.assertEqual(decision.resource_observation["source"], "node-exporter")

    def test_missing_resource_values_remain_unknown_and_keep_legacy_choice(self):
        cache = ObservationCache(ttl_s=30)
        cache.put(_observation("local-model", 40))
        cache.put(_observation("cloud-model", 100))

        decision = TaskRouter(cache).choose(_task(), now=101.0)

        self.assertEqual(decision.backend, "local-model")
        self.assertIsNone(decision.resource_observation["queue_ms"])
        self.assertIsNone(decision.resource_observation["free_mem_mb"])

    def test_switch_margin_prevents_route_flapping_for_small_latency_changes(self):
        cache = ObservationCache(ttl_s=30)
        cache.put(_observation("local-model", 100))
        cache.put(_observation("cloud-model", 102))
        router = TaskRouter(cache)
        self.assertEqual(router.choose(_task(), now=101.0).backend, "local-model")

        cache.put(_observation("local-model", 104, checked_at=102.0))
        cache.put(_observation("cloud-model", 100, checked_at=102.0))
        decision = router.choose(_task(), now=103.0)

        self.assertEqual(decision.backend, "local-model")
        self.assertIn("HYSTERESIS", decision.reason)

    def test_strict_privacy_never_uses_cloud_resource_capacity(self):
        cache = ObservationCache(ttl_s=30)
        cache.put(_observation("local-model", 40, quality=0.95, resources={
            "queue_ms": 600, "free_mem_mb": 64, "inflight": 20,
            "source": "node-exporter", "observed_at": 100.0,
        }))
        cache.put(_observation("cloud-model", 5, quality=1.0))

        decision = TaskRouter(cache, trusted_backends={"local-model"}).choose(
            _task(privacy="strict"), now=101.0
        )

        self.assertEqual(decision.backend, "unavailable")

    def test_resource_limits_and_timestamps_are_validated(self):
        cache = ObservationCache()
        for resource_factory in (
            lambda: ResourceObservation(queue_ms=-1, observed_at=100.0),
            lambda: ResourceObservation(free_mem_mb=float("nan"), observed_at=100.0),
            lambda: ResourceObservation(inflight=1.5, observed_at=100.0),
        ):
            with self.subTest(resource_factory=resource_factory), self.assertRaises(ValueError):
                cache.put(HealthObservation("local-model", True, 10, 0.9, 100.0, resources=resource_factory()))

    def test_runtime_reservations_shift_concurrent_requests_and_release(self):
        cache = ObservationCache(ttl_s=30)
        cache.put(_observation("local-model", 40))
        cache.put(_observation("cloud-model", 100))
        router = TaskRouter(cache, max_inflight=1)

        first, first_token = router.choose_and_reserve(_task(), now=101.0)
        second, second_token = router.choose_and_reserve(_task(), now=101.0)

        self.assertEqual(first.backend, "local-model")
        self.assertEqual(second.backend, "cloud-model")
        self.assertEqual(second.resource_observation["runtime_inflight"], 0)
        router.release(first_token)
        third, third_token = router.choose_and_reserve(_task(), now=101.0)
        self.assertEqual(third.backend, "local-model")
        router.release(third_token)
        router.release(second_token)
        router.release(first_token)

    def test_runtime_releases_reservation_when_backend_raises(self):
        cache = ObservationCache(ttl_s=30)
        cache.put(_observation("local-model", 40, checked_at=time.time()))
        router = TaskRouter(cache, max_inflight=1)

        class BrokenBackend:
            def infer(self, *_args, **_kwargs):
                raise RuntimeError("simulated endpoint failure")

        runtime = TaskRuntime(
            router=router,
            backends={"local-model": BrokenBackend()},
            trusted_backends={"local-model"},
        )

        result = runtime.handle("帮我把书房灯调到舒服的亮度", request_id="resource-release")

        self.assertFalse(result["ok"])
        self.assertEqual(router.inflight_snapshot(), {})


if __name__ == "__main__":
    unittest.main()
