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
            printf("ok\n");

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
                backend_t       b = g_compute_valid
                    ? policy_select_with_compute(&g_cfg, &g_net, &g_compute, t, &r)
                    : policy_select(&g_cfg, &g_net, t, &r);
                char log[512];
                if (g_compute_valid) {
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
