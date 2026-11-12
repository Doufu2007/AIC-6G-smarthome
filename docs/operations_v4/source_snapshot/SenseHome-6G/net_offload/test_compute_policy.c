#include <stdio.h>
#include "offload_policy.h"

static int failures = 0;

static void expect(int condition, const char *name)
{
    if (condition) printf("PASS  %s\n", name);
    else { printf("FAIL  %s\n", name); failures++; }
}

int main(void)
{
    policy_cfg_t cfg;
    net_state_t net;
    compute_state_t compute;
    policy_reason_t reason;
    backend_t backend;

    policy_cfg_default(&cfg);
    net_state_init(&net);
    link_update(&net.wan, 1, 30);
    link_update(&net.lan, 1, 3);

    compute_state_init(&compute);
    compute.model_ready = 1;
    compute.queue_ms = 0;
    compute.free_mem_mb = 4096;
    compute.inflight = 0;
    backend = policy_select_with_compute(&cfg, &net, &compute, TASK_COMPLEX_NLU, &reason);
    expect(backend == BE_CLOUD_MODEL && reason == REASON_WAN_OK,
           "healthy compute preserves cloud selection");

    compute.model_ready = 0;
    backend = policy_select_with_compute(&cfg, &net, &compute, TASK_COMPLEX_NLU, &reason);
    expect(backend == BE_LOCAL_MODEL && reason == REASON_MODEL_UNAVAILABLE,
           "unavailable cloud model falls back to local model");

    compute.model_ready = 1;
    compute.queue_ms = 500;
    backend = policy_select_with_compute(&cfg, &net, &compute, TASK_COMPLEX_NLU, &reason);
    expect(backend == BE_LOCAL_MODEL && reason == REASON_COMPUTE_OVERLOADED,
           "overloaded compute falls back to local model");

    backend = policy_select_with_compute(&cfg, &net, &compute, TASK_SIMPLE_CONTROL, &reason);
    expect(backend == BE_LOCAL_RULES && reason == REASON_TASK_IS_SIMPLE,
           "simple task remains local rules under compute overload");

    backend = policy_select_with_compute(&cfg, &net, &compute, TASK_SAFETY_CRITICAL, &reason);
    expect(backend == BE_LOCAL_RULES && reason == REASON_TASK_IS_SAFETY,
           "safety task remains local rules under compute overload");

    /* Thresholds are inclusive on the healthy side. */
    compute.queue_ms = OFFLOAD_COMPUTE_QUEUE_MAX_MS;
    compute.free_mem_mb = OFFLOAD_COMPUTE_FREE_MEM_MIN_MB;
    compute.inflight = OFFLOAD_COMPUTE_INFLIGHT_MAX;
    backend = policy_select_with_compute(&cfg, &net, &compute, TASK_COMPLEX_NLU, &reason);
    expect(backend == BE_CLOUD_MODEL && reason == REASON_WAN_OK,
           "compute thresholds exactly at budget preserve cloud selection");

    compute.free_mem_mb = OFFLOAD_COMPUTE_FREE_MEM_MIN_MB - 1;
    backend = policy_select_with_compute(&cfg, &net, &compute, TASK_COMPLEX_NLU, &reason);
    expect(backend == BE_LOCAL_MODEL && reason == REASON_COMPUTE_OVERLOADED,
           "one megabyte below compute budget falls back");

    compute.free_mem_mb = OFFLOAD_COMPUTE_FREE_MEM_MIN_MB;
    compute.inflight = OFFLOAD_COMPUTE_INFLIGHT_MAX + 1;
    backend = policy_select_with_compute(&cfg, &net, &compute, TASK_COMPLEX_NLU, &reason);
    expect(backend == BE_LOCAL_MODEL && reason == REASON_COMPUTE_OVERLOADED,
           "one request above concurrency budget falls back");

    /* Existing network reasons take precedence over compute health. */
    net.wan.reachable = 0;
    compute.model_ready = 0;
    backend = policy_select_with_compute(&cfg, &net, &compute, TASK_COMPLEX_NLU, &reason);
    expect(backend == BE_LOCAL_MODEL && reason == REASON_WAN_UNREACHABLE,
           "WAN failure reason is preserved when compute is also unhealthy");

    net.lan.reachable = 0;
    backend = policy_select_with_compute(&cfg, &net, &compute, TASK_COMPLEX_NLU, &reason);
    expect(backend == BE_LOCAL_RULES && reason == REASON_LAN_DOWN,
           "LAN failure forces local rules regardless of compute");

    backend = policy_select_with_compute(&cfg, NULL, NULL, TASK_COMPLEX_NLU, &reason);
    expect(backend == BE_LOCAL_RULES && reason == REASON_LAN_DOWN,
           "NULL network and compute inputs use conservative defaults");

    backend = policy_select_with_compute(&cfg, &net, NULL, TASK_SIMPLE_CONTROL, &reason);
    expect(backend == BE_LOCAL_RULES && reason == REASON_TASK_IS_SIMPLE,
           "NULL compute does not affect simple task routing");

    net.wan.reachable = 1;
    net.lan.reachable = 1;
    compute_state_unknown(&compute);
    expect(compute.model_ready == -1 && compute.queue_ms == -1 &&
           compute.free_mem_mb == -1 && compute.inflight == -1,
           "unknown compute metrics have explicit -1 sentinel");
    compute.model_ready = 0;
    backend = policy_select_with_compute(&cfg, &net, &compute, TASK_COMPLEX_NLU, &reason);
    expect(backend == BE_LOCAL_MODEL && reason == REASON_MODEL_UNAVAILABLE,
           "known unavailable model routes locally when memory and queue are unknown");
    compute.model_ready = 1;
    backend = policy_select_with_compute(&cfg, &net, &compute, TASK_COMPLEX_NLU, &reason);
    expect(backend == BE_CLOUD_MODEL && reason == REASON_WAN_OK,
           "unknown memory is not treated as observed low memory");
    compute.model_ready = -1;
    compute.inflight = OFFLOAD_COMPUTE_INFLIGHT_MAX + 1;
    backend = policy_select_with_compute(&cfg, &net, &compute, TASK_COMPLEX_NLU, &reason);
    expect(backend == BE_LOCAL_MODEL && reason == REASON_COMPUTE_OVERLOADED,
           "known inflight overload is actionable without model-ready or memory telemetry");
    compute.inflight = -1;
    backend = policy_select_with_compute(&cfg, &net, &compute, TASK_COMPLEX_NLU, &reason);
    expect(backend == BE_CLOUD_MODEL && reason == REASON_WAN_OK,
           "all unknown compute leaves link policy unchanged");

    printf("=== compute policy: %d failed ===\n", failures);
    return failures ? 1 : 0;
}
