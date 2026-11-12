/*
 * policy_cli.c — 卸载策略的行协议驱动（测量脚手架用）
 *
 * 存在理由：Python 测量脚本**不得**重新实现一遍决策表。
 * 一旦两处各自维护同一张表，测出来的就只是"两份实现碰巧一致"，
 * 而不是被测系统的行为。所以这里让 C 侧常驻一个进程，
 * Python 只喂观测样本、只读决策结果 —— 策略的唯一权威实现在
 * offload_policy.c，平滑状态也留在 C 侧，跨阶段连续。
 *
 * 行协议（stdin 一行 → stdout 一行）：
 *   probe <wan|lan> <ok 0|1> <rtt_ms>   -> link <link> <rtt> <jit> <loss> <samples>
 *   cfg <rtt_max> <jit_max> <loss_max>  -> ok
 *   compute <ready> <queue_ms> <free_mem_mb> <inflight> -> ok
 *   compute_partial <ready|-1> <queue|-1> <free_mem|-1> <inflight|-1> -> ok
 *   decide <simple|complex|safety>      -> decision <backend> <reason> <log...>
 *   reset                               -> ok
 *   quit                                -> (退出)
 * 无法识别的输入一律回 err，不中断进程。
 *
 * 编译： gcc -std=c99 -Wall -Wextra -O2 -o policy_cli policy_cli.c offload_policy.c
 */
#include <stdio.h>
#include <string.h>
#include "offload_policy.h"

#define LINE_MAX 512

static policy_cfg_t g_cfg;
static net_state_t  g_net;
static compute_state_t g_compute;
static int g_compute_valid;
static resource_cfg_t g_resources;
static resource_history_t g_history;
static compute_state_t g_local, g_cloud;
static int g_resources_valid;

static task_class_t parse_task(const char *s)
{
    if (!s) return TASK_SIMPLE_CONTROL;
    if (strcmp(s, "complex") == 0) return TASK_COMPLEX_NLU;
    if (strcmp(s, "safety")  == 0) return TASK_SAFETY_CRITICAL;
    return TASK_SIMPLE_CONTROL;
}

int main(void)
{
    char line[LINE_MAX];
    char cmd[32];

    policy_cfg_default(&g_cfg);
    net_state_init(&g_net);
    compute_state_init(&g_compute);
    g_compute_valid = 0;
    resource_cfg_default(&g_resources);
    resource_history_init(&g_history);
    compute_state_unknown(&g_local);
    compute_state_unknown(&g_cloud);
    g_resources_valid = 0;

    printf("ready\n");
    fflush(stdout);

    while (fgets(line, sizeof line, stdin)) {

        if (sscanf(line, "%31s", cmd) != 1) { printf("err\n"); fflush(stdout); continue; }

        if (strcmp(cmd, "quit") == 0) {
            break;

        } else if (strcmp(cmd, "reset") == 0) {
            policy_cfg_default(&g_cfg);
            net_state_init(&g_net);
            compute_state_init(&g_compute);
            g_compute_valid = 0;
            resource_cfg_default(&g_resources);
            resource_history_init(&g_history);
            compute_state_unknown(&g_local);
            compute_state_unknown(&g_cloud);
            g_resources_valid = 0;
            printf("ok\n");

        } else if (strcmp(cmd, "resource_cfg") == 0) {
            resource_cfg_t c;
            resource_cfg_default(&c);
            if (sscanf(line, "%*s %d %d %d %d %d %d %d %d %d %d %d %d %d",
                       &c.network.cloud_rtt_max_ms, &c.network.cloud_jitter_max_ms,
                       &c.network.cloud_loss_max_pct, &c.queue_max_ms, &c.inflight_max,
                       &c.free_mem_min_mb, &c.local_infer_ms, &c.cloud_infer_ms,
                       &c.local_transfer_ms, &c.cloud_transfer_ms, &c.switch_margin_ms,
                       &c.recovery_samples, &c.mode) == 13 &&
                c.network.cloud_rtt_max_ms >= 0 && c.network.cloud_rtt_max_ms <= 60000 &&
                c.network.cloud_jitter_max_ms >= 0 && c.network.cloud_jitter_max_ms <= 60000 &&
                c.network.cloud_loss_max_pct >= 0 && c.network.cloud_loss_max_pct <= 100 &&
                c.queue_max_ms >= 0 && c.queue_max_ms <= 1000000 &&
                c.inflight_max >= 0 && c.inflight_max <= 1000000 &&
                c.free_mem_min_mb >= 0 && c.free_mem_min_mb <= 1000000 &&
                c.local_infer_ms >= 0 && c.local_infer_ms <= 1000000 &&
                c.cloud_infer_ms >= 0 && c.cloud_infer_ms <= 1000000 &&
                c.local_transfer_ms >= -1 && c.local_transfer_ms <= 1000000 &&
                c.cloud_transfer_ms >= -1 && c.cloud_transfer_ms <= 1000000 &&
                c.switch_margin_ms >= 0 && c.switch_margin_ms <= 1000000 &&
                c.recovery_samples >= 0 && c.recovery_samples <= 10000 &&
                c.mode >= 0 && c.mode <= 5) {
                g_resources = c;
                g_resources_valid = 1;
                printf("ok\n");
            } else {
                printf("err\n");
            }

        } else if (strcmp(cmd, "resource_tick") == 0) {
            if (g_history.observation_generation < 1000000000)
                g_history.observation_generation++;
            printf("ok\n");

        } else if (strcmp(cmd, "resource_clear") == 0) {
            int clear;
            if (sscanf(line, "%*s %d", &clear) == 1 && clear == 1) {
                g_resources_valid = 0;
                g_compute_valid = 0;
                compute_state_unknown(&g_local);
                compute_state_unknown(&g_cloud);
                compute_state_init(&g_compute);
                resource_history_init(&g_history);
                printf("ok\n");
            } else {
                printf("err\n");
            }

        } else if (strcmp(cmd, "endpoint") == 0) {
            char name[16];
            int ready, q, mem, n;
            if (sscanf(line, "%*s %15s %d %d %d %d", name, &ready, &q, &mem, &n) == 5 &&
                (strcmp(name, "local") == 0 || strcmp(name, "cloud") == 0) &&
                ready >= -1 && ready <= 1 && q >= -1 && q <= 1000000 &&
                mem >= -1 && mem <= 1000000 && n >= -1 && n <= 1000000) {
                compute_state_t *state = strcmp(name, "local") == 0 ? &g_local : &g_cloud;
                state->model_ready = ready;
                state->queue_ms = q;
                state->free_mem_mb = mem;
                state->inflight = n;
                printf("ok\n");
            } else {
                printf("err\n");
            }

        } else if (strcmp(cmd, "observe") == 0) {
            char name[16];
            int ok, rtt, jitter, loss;
            if (sscanf(line, "%*s %15s %d %d %d %d", name, &ok, &rtt, &jitter, &loss) == 5 &&
                (strcmp(name, "wan") == 0 || strcmp(name, "lan") == 0) &&
                (ok == 0 || ok == 1) && rtt >= -1 && rtt <= 60000 &&
                jitter >= -1 && jitter <= 60000 && loss >= -1 && loss <= 100) {
                link_state_t *state = strcmp(name, "wan") == 0 ? &g_net.wan : &g_net.lan;
                link_update(state, ok, rtt < 0 ? 0 : rtt);
                state->rtt_known = rtt >= 0;
                state->jitter_known = jitter >= 0;
                state->loss_known = loss >= 0;
                state->rtt_ms = rtt;
                state->jitter_ms = jitter;
                state->loss_pct = loss;
                printf("ok\n");
            } else {
                printf("err\n");
            }

        } else if (strcmp(cmd, "cfg") == 0) {
            int rtt, jit, loss;
            if (sscanf(line, "%*s %d %d %d", &rtt, &jit, &loss) == 3) {
                g_cfg.cloud_rtt_max_ms    = rtt;
                g_cfg.cloud_jitter_max_ms = jit;
                g_cfg.cloud_loss_max_pct  = loss;
                printf("ok\n");
            } else {
                printf("err\n");
            }

        } else if (strcmp(cmd, "compute") == 0) {
            int ready, queue_ms, free_mem_mb, inflight;
            if (sscanf(line, "%*s %d %d %d %d", &ready, &queue_ms, &free_mem_mb, &inflight) == 4 &&
                (ready == 0 || ready == 1) && queue_ms >= 0 && free_mem_mb >= 0 && inflight >= 0) {
                g_compute.model_ready = ready ? 1 : 0;
                g_compute.queue_ms = queue_ms < 0 ? 0 : queue_ms;
                g_compute.free_mem_mb = free_mem_mb < 0 ? 0 : free_mem_mb;
                g_compute.inflight = inflight < 0 ? 0 : inflight;
                g_compute_valid = 1;
                printf("ok\n");
            } else {
                printf("err\n");
            }

        } else if (strcmp(cmd, "compute_partial") == 0) {
            int ready, queue_ms, free_mem_mb, inflight;
            if (sscanf(line, "%*s %d %d %d %d", &ready, &queue_ms, &free_mem_mb, &inflight) == 4 &&
                ready >= -1 && ready <= 1 && queue_ms >= -1 && free_mem_mb >= -1 && inflight >= -1) {
                compute_state_unknown(&g_compute);
                g_compute.model_ready = ready;
                g_compute.queue_ms = queue_ms;
                g_compute.free_mem_mb = free_mem_mb;
                g_compute.inflight = inflight;
                g_compute_valid = 1;
                printf("ok\n");
            } else {
                printf("err\n");
            }

        } else if (strcmp(cmd, "probe") == 0) {
            char link[16];
            int  okflag, rtt;
            if (sscanf(line, "%*s %15s %d %d", link, &okflag, &rtt) == 3) {
                link_state_t *st = (strcmp(link, "lan") == 0) ? &g_net.lan : &g_net.wan;
                link_update(st, okflag, rtt);
                printf("link %s %d %d %d %d\n",
                       link, st->rtt_ms, st->jitter_ms, st->loss_pct, st->samples);
            } else {
                printf("err\n");
            }

        } else if (strcmp(cmd, "decide") == 0) {
            char tn[32];
            if (sscanf(line, "%*s %31s", tn) == 1) {
                task_class_t    t = parse_task(tn);
                policy_reason_t r = REASON_WAN_OK;
                backend_t       b = g_resources_valid
                    ? policy_select_resources(&g_resources, &g_net, &g_local, &g_cloud, &g_history, t, &r)
                    : g_compute_valid
                    ? policy_select_with_compute(&g_cfg, &g_net, &g_compute, t, &r)
                    : policy_select(&g_cfg, &g_net, t, &r);
                char log[512];
                if (g_resources_valid) {
                    snprintf(log, sizeof log,
                             "[resources] task=%s -> %s because %s mode=%d local(ready=%d,q=%d,mem=%d,n=%d) cloud(ready=%d,q=%d,mem=%d,n=%d) transfer_ms(local=%d,cloud=%d;-1=unknown) wan(rtt=%d,jit=%d,loss=%d) lan(rtt=%d,jit=%d,loss=%d) recovery=%d/%d",
                             task_class_name(t), backend_name(b), policy_reason_name(r), g_resources.mode,
                             g_local.model_ready, g_local.queue_ms, g_local.free_mem_mb, g_local.inflight,
                             g_cloud.model_ready, g_cloud.queue_ms, g_cloud.free_mem_mb, g_cloud.inflight,
                             g_resources.local_transfer_ms, g_resources.cloud_transfer_ms,
                             g_net.wan.rtt_ms, g_net.wan.jitter_ms, g_net.wan.loss_pct,
                             g_net.lan.rtt_ms, g_net.lan.jitter_ms, g_net.lan.loss_pct,
                             g_history.cloud_good_samples, g_resources.recovery_samples);
                } else if (g_compute_valid) {
                    /* Explain the actual compute-aware selection, rather
                     * than recomputing the network-only legacy decision. */
                    snprintf(log, sizeof log,
                             "[offload] task=%s -> %s because %s compute(model_ready=%d,queue_ms=%d,free_mem_mb=%d,inflight=%d,unknown=-1)",
                             task_class_name(t), backend_name(b), policy_reason_name(r),
                             g_compute.model_ready, g_compute.queue_ms,
                             g_compute.free_mem_mb, g_compute.inflight);
                } else {
                    policy_explain(log, sizeof log, &g_cfg, &g_net, t);
                }
                printf("decision %s %s %s\n", backend_name(b), policy_reason_name(r), log);
            } else {
                printf("err\n");
            }

        } else {
            printf("err\n");
        }

        fflush(stdout);
    }

    return 0;
}
