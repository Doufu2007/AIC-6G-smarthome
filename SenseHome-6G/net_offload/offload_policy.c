/*
 * offload_policy.c — 链路感知的推理卸载策略（实现）
 *
 * 设计约束（与 openvela 核心栈一致）：
 *   - 无 malloc / 无浮点 / 无 <math.h>：全部整数运算
 *   - 无全局可变状态：所有状态由调用方持有，便于单元测试与多实例
 *   - 单个决策 O(1)，最坏路径 5 次比较
 */
#include "offload_policy.h"

/* ------------------------------------------------------------------ */
/* 指数加权平滑                                                        */
/*                                                                     */
/* 上升与下降刻意用不同的取整方式：                                      */
/*   上升： cur + (sample-cur)/2^k        —— 向零取整，慢一点没关系       */
/*   下降： cur - ceil((cur-sample)/2^k)  —— 必须用 ceil。              */
/*                                                                     */
/* ⚠️ 下降若也用向零取整会**卡死**：当 cur=7、sample=0 时 (0-7)/8 == 0， */
/*    丢包率将永远停在 7%，于是一条早已恢复的链路会被永久判为"劣化"。     */
/*    这是本模块唯一一处"看起来可以简化、实则不能"的地方。               */
/* ------------------------------------------------------------------ */
static int smooth_toward(int cur, int sample)
{
    if (sample > cur)
        return cur + (sample - cur) / (1 << OFFLOAD_EWM_SHIFT);
    if (sample < cur) {
        int d = cur - sample;
        int step = (d + (1 << OFFLOAD_EWM_SHIFT) - 1) >> OFFLOAD_EWM_SHIFT;
        return cur - step;
    }
    return cur;
}

/* ------------------------------------------------------------------ */
/* 初始化                                                              */
/* ------------------------------------------------------------------ */

void policy_cfg_default(policy_cfg_t *cfg)
{
    if (!cfg) return;
    /*
     * 时延阈值 300ms 的推导：ITU-T G.114 建议交互式语音单向时延
     * 控制在 150ms 以内，折算往返即 300ms。家庭语音/触控交互与之同级，
     * 故以此作为"WAN 是否还能承载云端大模型"的判据。
     * 抖动与丢包两项没有可直接引用的强制标准，是工程选取的设计参数，
     * 由 offload_bench 做阈值扫描，不宣称其为标准值。
     */
    cfg->cloud_rtt_max_ms    = 300;
    cfg->cloud_jitter_max_ms = 30;
    cfg->cloud_loss_max_pct  = 5;
    cfg->safety_local_always = 1;
}

void link_state_init(link_state_t *st)
{
    if (!st) return;
    st->reachable = 0;
    st->samples   = 0;
    st->rtt_ms    = 0;
    st->jitter_ms = 0;
    st->loss_pct  = 0;
    st->last_rtt  = 0;
    st->has_last  = 0;
}

void net_state_init(net_state_t *net)
{
    if (!net) return;
    link_state_init(&net->wan);
    link_state_init(&net->lan);
}

void compute_state_init(compute_state_t *state)
{
    if (!state) return;
    state->model_ready = 1;
    state->queue_ms = 0;
    state->free_mem_mb = OFFLOAD_COMPUTE_FREE_MEM_MIN_MB;
    state->inflight = 0;
}

void compute_state_unknown(compute_state_t *state)
{
    if (!state) return;
    state->model_ready = -1;
    state->queue_ms = -1;
    state->free_mem_mb = -1;
    state->inflight = -1;
}

/* ------------------------------------------------------------------ */
/* 链路状态更新                                                        */
/* ------------------------------------------------------------------ */

int link_update(link_state_t *st, int ok, int rtt_ms)
{
    if (!st) return 0;

    if (rtt_ms < 0) rtt_ms = 0;
    if (rtt_ms > 60000) rtt_ms = 60000;   /* 夹紧，防止一次异常探测污染平滑值 */

    /* 丢包：成功记 0、失败记 100，再平滑 */
    st->loss_pct = smooth_toward(st->loss_pct, ok ? 0 : 100);

    if (ok) {
        if (st->samples == 0 || !st->reachable) {
            /* 冷启动、或刚从断链恢复：直接采信首个样本。
             * 否则会从 0 / 旧值缓慢爬升，在恢复后的一段时间里持续误判。
             * 抖动同时清零：断链前后属于两段不同的链路状态，没有可比的
             * "相邻样本"，留旧值只会把恢复期拖长。 */
            st->rtt_ms    = rtt_ms;
            st->jitter_ms = 0;
        } else if (st->has_last) {
            /* 抖动 = 相邻两次原始样本之差的平滑值（RFC 3550 式） */
            int delta = rtt_ms - st->last_rtt;
            int absd  = (delta < 0) ? -delta : delta;
            st->jitter_ms = smooth_toward(st->jitter_ms, absd);
            st->rtt_ms    = smooth_toward(st->rtt_ms, rtt_ms);
        } else {
            /* 手工构造的状态没有历史样本，无法算相邻差 */
            st->rtt_ms    = smooth_toward(st->rtt_ms, rtt_ms);
            st->jitter_ms = 0;
        }
        st->last_rtt = rtt_ms;
        st->has_last = 1;
    }

    st->reachable = ok ? 1 : 0;
    st->samples++;
    return st->samples;
}

int link_is_usable(const link_state_t *st)
{
    return (st && st->samples > 0 && st->reachable) ? 1 : 0;
}

int link_meets_budget(const policy_cfg_t *cfg, const link_state_t *link)
{
    if (!cfg || !link) return 0;
    if (!link_is_usable(link)) return 0;
    if (link->rtt_ms    > cfg->cloud_rtt_max_ms)    return 0;
    if (link->jitter_ms > cfg->cloud_jitter_max_ms) return 0;
    if (link->loss_pct  > cfg->cloud_loss_max_pct)  return 0;
    return 1;
}

/* 返回 WAN 未达标的第一条原因；达标返回 REASON_WAN_OK */
static policy_reason_t wan_reason(const policy_cfg_t *cfg, const link_state_t *wan)
{
    if (wan->samples == 0)   return REASON_WAN_UNKNOWN;
    if (!wan->reachable)     return REASON_WAN_UNREACHABLE;
    if (wan->rtt_ms    > cfg->cloud_rtt_max_ms)    return REASON_WAN_RTT_HIGH;
    if (wan->jitter_ms > cfg->cloud_jitter_max_ms) return REASON_WAN_JITTER_HIGH;
    if (wan->loss_pct  > cfg->cloud_loss_max_pct)  return REASON_WAN_LOSS_HIGH;
    return REASON_WAN_OK;
}

/* ------------------------------------------------------------------ */
/* 核心决策                                                            */
/* ------------------------------------------------------------------ */

backend_t policy_select_with_compute(const policy_cfg_t *cfg,
                                     const net_state_t *net,
                                     const compute_state_t *compute,
                                     task_class_t task,
                                     policy_reason_t *reason)
{
    policy_cfg_t def;
    net_state_t  empty;          /* 必须留在函数作用域：net 会指向它 */
    compute_state_t healthy_compute;
    backend_t    be;
    policy_reason_t r;

    if (!cfg) { policy_cfg_default(&def); cfg = &def; }

    /* 无网络信息时按"两条链路都未知"处理，走最保守分支 */
    if (!net) { net_state_init(&empty); net = &empty; }
    if (!compute) { compute_state_init(&healthy_compute); compute = &healthy_compute; }

    switch (task) {

    case TASK_SAFETY_CRITICAL:
        if (cfg->safety_local_always) {
            be = BE_LOCAL_RULES;
            r  = REASON_TASK_IS_SAFETY;
            break;
        }
        /* safety_local_always=0 是留给消融实验的开关：
         * 让安全关键任务也走链路判据，用来量化"不走专用切片"的代价。 */
        /* fall through */
    case TASK_COMPLEX_NLU:
        if (!link_is_usable(&net->lan)) {
            /* WAN 再好也没用：Agent 连不到任何模型托管方 */
            be = BE_LOCAL_RULES;
            r  = REASON_LAN_DOWN;
            break;
        }
        r = wan_reason(cfg, &net->wan);
        if (r == REASON_WAN_OK) {
            if (compute->model_ready == 0) {
                be = BE_LOCAL_MODEL;
                r = REASON_MODEL_UNAVAILABLE;
            } else if (compute->queue_ms > OFFLOAD_COMPUTE_QUEUE_MAX_MS ||
                       (compute->free_mem_mb >= 0 &&
                        compute->free_mem_mb < OFFLOAD_COMPUTE_FREE_MEM_MIN_MB) ||
                       compute->inflight > OFFLOAD_COMPUTE_INFLIGHT_MAX) {
                be = BE_LOCAL_MODEL;
                r = REASON_COMPUTE_OVERLOADED;
            } else {
                be = BE_CLOUD_MODEL;
            }
        } else {
            /* WAN 未知/断/超阈 —— 全部降到网关本地小模型。
             * 四种子原因在日志里区分，便于统计"降级是被什么触发的"。 */
            be = BE_LOCAL_MODEL;
        }
        break;

    case TASK_SIMPLE_CONTROL:
    default:
        be = BE_LOCAL_RULES;
        r  = REASON_TASK_IS_SIMPLE;
        break;
    }

    if (reason) *reason = r;
    return be;
}

backend_t policy_select(const policy_cfg_t *cfg,
                        const net_state_t *net,
                        task_class_t task,
                        policy_reason_t *reason)
{
    compute_state_t healthy_compute;
    compute_state_init(&healthy_compute);
    return policy_select_with_compute(cfg, net, &healthy_compute, task, reason);
}

/* ------------------------------------------------------------------ */
/* 名字表                                                              */
/* ------------------------------------------------------------------ */

const char *backend_name(backend_t b)
{
    switch (b) {
    case BE_LOCAL_RULES: return "local-rules";
    case BE_LOCAL_MODEL: return "local-model";
    case BE_CLOUD_MODEL: return "cloud-model";
    case BE_NONE:
    default:             return "none";
    }
}

const char *task_class_name(task_class_t t)
{
    switch (t) {
    case TASK_SIMPLE_CONTROL:  return "simple-control";
    case TASK_COMPLEX_NLU:     return "complex-nlu";
    case TASK_SAFETY_CRITICAL: return "safety-critical";
    default:                   return "unknown-task";
    }
}

const char *policy_reason_name(policy_reason_t r)
{
    switch (r) {
    case REASON_WAN_OK:          return "WAN_OK";
    case REASON_WAN_UNKNOWN:     return "WAN_UNKNOWN";
    case REASON_WAN_UNREACHABLE: return "WAN_UNREACHABLE";
    case REASON_WAN_RTT_HIGH:    return "WAN_RTT_HIGH";
    case REASON_WAN_JITTER_HIGH: return "WAN_JITTER_HIGH";
    case REASON_WAN_LOSS_HIGH:   return "WAN_LOSS_HIGH";
    case REASON_LAN_DOWN:        return "LAN_DOWN";
    case REASON_TASK_IS_SIMPLE:  return "TASK_IS_SIMPLE";
    case REASON_TASK_IS_SAFETY:  return "TASK_IS_SAFETY";
    case REASON_MODEL_UNAVAILABLE:return "MODEL_UNAVAILABLE";
    case REASON_COMPUTE_OVERLOADED:return "COMPUTE_OVERLOADED";
    default:                     return "UNKNOWN";
    }
}

/* ------------------------------------------------------------------ */
/* 日志渲染                                                            */
/* 不依赖 snprintf，避免在部分 RTOS 的 libc 配置下不可用。              */
/* ------------------------------------------------------------------ */

typedef struct {
    char *buf;
    int   cap;   /* 可写容量（含结尾 NUL 的预留位） */
    int   len;   /* 已写字符数 */
} sbuf_t;

static void sb_putc(sbuf_t *sb, char c)
{
    if (sb->len < sb->cap - 1) {
        sb->buf[sb->len] = c;
        sb->len++;
        sb->buf[sb->len] = '\0';
    } else {
        sb->len++;          /* 仍计数，便于调用方判断是否被截断 */
    }
}

static void sb_puts(sbuf_t *sb, const char *s)
{
    if (!s) s = "(null)";
    while (*s) sb_putc(sb, *s++);
}

static void sb_putint(sbuf_t *sb, int v)
{
    char tmp[12];
    int  n = 0;
    unsigned int u;

    if (v < 0) { sb_putc(sb, '-'); u = (unsigned int)(-(long)v); }
    else       { u = (unsigned int)v; }

    do { tmp[n++] = (char)('0' + (u % 10u)); u /= 10u; } while (u);
    while (n > 0) sb_putc(sb, tmp[--n]);
}

int policy_explain(char *buf, int buflen,
                   const policy_cfg_t *cfg,
                   const net_state_t *net,
                   task_class_t task)
{
    sbuf_t sb;
    policy_reason_t r = REASON_WAN_OK;
    backend_t be;

    if (!buf || buflen <= 0) return -1;

    be = policy_select(cfg, net, task, &r);

    sb.buf = buf; sb.cap = buflen; sb.len = 0;
    buf[0] = '\0';

    sb_puts(&sb, "[offload] task=");
    sb_puts(&sb, task_class_name(task));

    sb_puts(&sb, " wan(rtt=");
    sb_putint(&sb, net ? net->wan.rtt_ms : 0);
    sb_puts(&sb, "ms,jit=");
    sb_putint(&sb, net ? net->wan.jitter_ms : 0);
    sb_puts(&sb, "ms,loss=");
    sb_putint(&sb, net ? net->wan.loss_pct : 0);
    sb_puts(&sb, "%,n=");
    sb_putint(&sb, net ? net->wan.samples : 0);
    sb_putc(&sb, ')');

    sb_puts(&sb, " lan(");
    sb_puts(&sb, link_is_usable(net ? &net->lan : 0) ? "up" : "down");
    sb_putc(&sb, ')');

    sb_puts(&sb, " -> ");
    sb_puts(&sb, backend_name(be));
    sb_puts(&sb, " because ");
    sb_puts(&sb, policy_reason_name(r));

    return sb.len;
}
