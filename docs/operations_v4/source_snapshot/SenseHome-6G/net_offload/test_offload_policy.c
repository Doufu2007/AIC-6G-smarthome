/*
 * test_offload_policy.c — 卸载策略单元测试
 *
 * 与 openvela_smart_home 既有的测试习惯一致：纯 C、零依赖、
 * 打印逐条 PASS/FAIL，失败以非零码退出（可直接挂进 CI）。
 *
 * 编译： gcc -std=c99 -Wall -Wextra -O2 -o test_offload_policy \
 *            test_offload_policy.c offload_policy.c
 * 运行： ./test_offload_policy
 */
#include <stdio.h>
#include <string.h>
#include "offload_policy.h"

static int g_pass = 0;
static int g_fail = 0;

static void ok(int cond, const char *what)
{
    if (cond) { g_pass++; printf("PASS  %s\n", what); }
    else      { g_fail++; printf("FAIL  %s\n", what); }
}

/* 直接构造一条链路快照（绕开平滑，用于精确测决策表） */
static void set_link(link_state_t *st, int reachable, int rtt,
                     int jit, int loss, int samples)
{
    st->reachable = reachable;
    st->rtt_ms    = rtt;
    st->jitter_ms = jit;
    st->loss_pct  = loss;
    st->samples   = samples;
    st->last_rtt  = rtt;
    st->has_last  = 1;
}

/* 理想 WAN：可达、时延抖动丢包全为 0 */
static link_state_t perfect_wan(void)
{
    link_state_t s;
    set_link(&s, 1, 20, 2, 0, 10);
    return s;
}

static link_state_t up_lan(void)
{
    link_state_t s;
    set_link(&s, 1, 1, 0, 0, 10);
    return s;
}

/* ================================================================== */
/* 组 1：配置与初始化                                                  */
/* ================================================================== */
static void test_cfg_and_init(void)
{
    policy_cfg_t cfg;
    link_state_t l;
    net_state_t  n;

    policy_cfg_default(&cfg);
    ok(cfg.cloud_rtt_max_ms > 0 && cfg.cloud_jitter_max_ms > 0 &&
       cfg.cloud_loss_max_pct > 0,
       "默认阈值全为正数");
    ok(cfg.cloud_loss_max_pct <= 100, "默认丢包阈值不超过 100%");
    ok(cfg.safety_local_always == 1, "默认强制安全关键任务走本机");

    link_state_init(&l);
    ok(l.samples == 0 && l.reachable == 0, "链路初始化为未知态");
    ok(!link_is_usable(&l), "未知链路不可用");

    net_state_init(&n);
    ok(!link_is_usable(&n.wan) && !link_is_usable(&n.lan),
       "网络全景初始化的两条链路都不可用");
}

/* ================================================================== */
/* 组 2：链路平滑（EWM）                                               */
/* ================================================================== */
static void test_link_update(void)
{
    link_state_t l;

    /* 冷启动采信首样本 */
    link_state_init(&l);
    ok(link_update(&l, 1, 137) == 1, "首次探测后 samples==1");
    ok(l.rtt_ms == 137 && l.reachable == 1, "冷启动直接采信首个 RTT 样本");
    ok(l.jitter_ms == 0, "冷启动抖动为 0");

    /* 连续失败 */
    {
        link_state_t f;
        link_state_init(&f);
        link_update(&f, 1, 20);
        link_update(&f, 0, 0);
        ok(f.reachable == 0, "探测失败后链路标记为不可达");
        ok(f.samples == 2, "失败也计入采样次数");
        ok(f.loss_pct > 0, "一次失败即抬升平滑丢包率");
        ok(!link_is_usable(&f), "不可达链路不可用");
    }

    /* 平滑收敛：40 次恒定样本后误差不超过步长上界 7ms */
    {
        int i;
        link_state_t c;
        link_state_init(&c);
        link_update(&c, 1, 100);            /* 冷启动取 100 */
        for (i = 0; i < 40; i++) link_update(&c, 1, 200);
        ok(c.rtt_ms >= 193 && c.rtt_ms <= 200,
           "RTT 平滑向 200ms 收敛（整数除法下界 193）");
    }

    /* ⭐ 回归测试：丢包平滑必须能回落到 0
     * 若下降沿用向零取整，loss 会永久卡在 7%，恢复后的链路被误判为劣化。 */
    {
        int i;
        link_state_t d;
        link_state_init(&d);
        for (i = 0; i < 5; i++) link_update(&d, 0, 0);
        ok(d.loss_pct > 40, "五次连续失败后丢包率显著抬升");
        for (i = 0; i < 40; i++) link_update(&d, 1, 20);
        ok(d.loss_pct == 0,
           "链路恢复后平滑丢包率能回落到 0（下降沿取整回归测试）");
    }

    /* 断链后恢复：立刻采信新样本，不从旧值缓慢爬 */
    {
        link_state_t r;
        link_state_init(&r);
        link_update(&r, 1, 100);
        link_update(&r, 0, 0);
        link_update(&r, 1, 500);
        ok(r.rtt_ms == 500 && r.reachable == 1,
           "断链恢复后直接采信新样本，不残留旧 RTT");
    }

    /* 异常值夹紧 */
    {
        link_state_t x;
        link_state_init(&x);
        link_update(&x, 1, -50);
        ok(x.rtt_ms == 0, "负 RTT 被夹紧为 0");

        link_state_init(&x);
        link_update(&x, 1, 999999);
        ok(x.rtt_ms == 60000, "超大 RTT 被夹紧为 60000ms");
    }

    ok(link_update(NULL, 1, 10) == 0, "link_update(NULL) 安全返回");
}

/* ================================================================== */
/* 组 2b：抖动估计量的回归测试                                          */
/*                                                                     */
/* 背景：抖动原先定义为 |本次样本 − 平滑值|。台阶式跳变（断链恢复）会被  */
/* 记成数百毫秒的"抖动"，再花几十个样本衰减 —— 实测后果是链路早已恢复，  */
/* 策略却长时间出不来降级档。改为相邻原始样本之差后必须守住下列性质。     */
/* ================================================================== */
static void test_jitter_estimator(void)
{
    link_state_t j;
    int i;

    /* 恒定 RTT → 抖动恒为 0 */
    link_state_init(&j);
    for (i = 0; i < 20; i++) link_update(&j, 1, 30);
    ok(j.jitter_ms == 0, "恒定 RTT 下抖动恒为 0");

    /* 一次台阶跳变：应当被如实记为抖动 —— 相邻样本确实差了 570ms。
     * （这一点不是缺陷：真实的链路跳变就该被察觉。） */
    link_update(&j, 1, 600);
    ok(j.jitter_ms > 30, "单次台阶跳变被如实记为抖动");

    /* 关键性质：台阶之后只要链路稳住，抖动必须在有限样本内衰减回去。
     * 旧的 |样本−平滑值| 估计量正是在这里失效的。 */
    for (i = 0; i < 15; i++) link_update(&j, 1, 600);
    ok(j.jitter_ms < 30, "台阶后链路稳住，抖动在 15 个样本内回落到阈值以下");

    /* 真实抖动（相邻样本持续跳动）仍必须被识别出来 —— 估计量不能被改瞎 */
    {
        link_state_t v;
        link_state_init(&v);
        for (i = 0; i < 30; i++) link_update(&v, 1, (i % 2) ? 20 : 220);
        ok(v.jitter_ms > 30, "相邻样本持续大幅跳动时抖动仍被识别");
    }

    /* 断链恢复：抖动清零、RTT 立刻采信新样本 */
    {
        link_state_t r;
        link_state_init(&r);
        for (i = 0; i < 20; i++) link_update(&r, 1, 30);
        link_update(&r, 0, 0);
        link_update(&r, 1, 30);
        ok(r.jitter_ms == 0, "断链恢复后抖动清零");
        ok(r.rtt_ms == 30, "断链恢复后 RTT 立刻采信新样本");
        ok(r.has_last == 1 && r.last_rtt == 30, "恢复后历史样本被更新");
    }

    /* ⭐ 核心回归：断链恢复后，链路必须能重新回到上云预算内。
     * 旧估计量下 jitter 被台阶撑到数百毫秒、且在整段恢复过程中维持高位，
     * 实测 45 次探测都没能恢复 —— 这才是真正要守住的性质。 */
    {
        link_state_t r;
        net_state_t  ns;
        policy_cfg_t cfg;
        policy_reason_t why;
        int recovered_at = -1;

        policy_cfg_default(&cfg);
        link_state_init(&r);
        for (i = 0; i < 10; i++) link_update(&r, 1, 30);
        for (i = 0; i < 6; i++) link_update(&r, 0, 0);       /* 断链 6 次探测 */

        for (i = 0; i < 60; i++) {                            /* 恢复：链路回到 30ms */
            link_update(&r, 1, 30);
            if (recovered_at < 0 && link_meets_budget(&cfg, &r))
                recovered_at = i;
        }
        ok(recovered_at >= 0 && recovered_at < 40,
           "断链恢复后链路在 40 个样本内重回上云预算");

        ns.wan = r; ns.lan = r;
        ok(policy_select(&cfg, &ns, TASK_COMPLEX_NLU, &why) == BE_CLOUD_MODEL,
           "恢复后复杂任务重新判定为上云（降级不是不可逆的）");
    }
}

/* ================================================================== */
/* 组 3：上云预算判定                                                  */
/* ================================================================== */
static void test_budget(void)
{
    policy_cfg_t cfg;
    link_state_t l;

    policy_cfg_default(&cfg);

    l = perfect_wan();
    ok(link_meets_budget(&cfg, &l), "理想链路满足上云预算");

    l = perfect_wan();
    l.rtt_ms = cfg.cloud_rtt_max_ms;
    ok(link_meets_budget(&cfg, &l), "RTT 恰好等于阈值仍视为达标（含等号）");

    l = perfect_wan();
    l.rtt_ms = cfg.cloud_rtt_max_ms + 1;
    ok(!link_meets_budget(&cfg, &l), "RTT 超阈值 1ms 即不达标");

    l = perfect_wan();
    l.jitter_ms = cfg.cloud_jitter_max_ms + 1;
    ok(!link_meets_budget(&cfg, &l), "抖动超阈不达标");

    l = perfect_wan();
    l.loss_pct = cfg.cloud_loss_max_pct + 1;
    ok(!link_meets_budget(&cfg, &l), "丢包超阈不达标");

    l = perfect_wan();
    l.reachable = 0;
    ok(!link_meets_budget(&cfg, &l), "不可达链路不满足预算");

    ok(!link_meets_budget(NULL, &l), "link_meets_budget(NULL cfg) 安全返回");
}

/* ================================================================== */
/* 组 4：决策表（7 行逐条验证）                                        */
/* ================================================================== */
static void test_decision_table(void)
{
    policy_cfg_t cfg;
    net_state_t  n;
    policy_reason_t r;
    backend_t    b;

    policy_cfg_default(&cfg);

    /* --- 安全关键：恒走本机，与链路无关 --- */
    n.wan = perfect_wan(); n.lan = up_lan();
    b = policy_select(&cfg, &n, TASK_SAFETY_CRITICAL, &r);
    ok(b == BE_LOCAL_RULES && r == REASON_TASK_IS_SAFETY,
       "安全关键 + 理想链路 → 仍走本机规则");

    n.wan = perfect_wan(); n.lan = up_lan(); n.lan.reachable = 0;
    b = policy_select(&cfg, &n, TASK_SAFETY_CRITICAL, &r);
    ok(b == BE_LOCAL_RULES && r == REASON_TASK_IS_SAFETY,
       "安全关键 + LAN 断开 → 仍走本机规则");

    /* --- 简单指令：不需要大模型 --- */
    n.wan = perfect_wan(); n.lan = up_lan();
    b = policy_select(&cfg, &n, TASK_SIMPLE_CONTROL, &r);
    ok(b == BE_LOCAL_RULES && r == REASON_TASK_IS_SIMPLE,
       "结构化指令 + 理想链路 → 仍走本机规则（不浪费算力）");

    /* --- 复杂 NLU：随链路浮动 --- */
    n.wan = perfect_wan(); n.lan = up_lan();
    b = policy_select(&cfg, &n, TASK_COMPLEX_NLU, &r);
    ok(b == BE_CLOUD_MODEL && r == REASON_WAN_OK,
       "复杂 NLU + WAN 达标 → 云端大模型");

    link_state_init(&n.wan); n.lan = up_lan();
    b = policy_select(&cfg, &n, TASK_COMPLEX_NLU, &r);
    ok(b == BE_LOCAL_MODEL && r == REASON_WAN_UNKNOWN,
       "复杂 NLU + WAN 未采样 → 保守降到网关本地模型");

    n.wan = perfect_wan(); n.wan.reachable = 0; n.lan = up_lan();
    b = policy_select(&cfg, &n, TASK_COMPLEX_NLU, &r);
    ok(b == BE_LOCAL_MODEL && r == REASON_WAN_UNREACHABLE,
       "复杂 NLU + WAN 断链 → 网关本地模型（LAN 仍可用）");

    n.wan = perfect_wan(); n.wan.rtt_ms = 900; n.lan = up_lan();
    b = policy_select(&cfg, &n, TASK_COMPLEX_NLU, &r);
    ok(b == BE_LOCAL_MODEL && r == REASON_WAN_RTT_HIGH,
       "复杂 NLU + WAN 时延超阈 → 网关本地模型");

    n.wan = perfect_wan(); n.wan.jitter_ms = 200; n.lan = up_lan();
    b = policy_select(&cfg, &n, TASK_COMPLEX_NLU, &r);
    ok(b == BE_LOCAL_MODEL && r == REASON_WAN_JITTER_HIGH,
       "复杂 NLU + WAN 抖动超阈 → 网关本地模型");

    n.wan = perfect_wan(); n.wan.loss_pct = 30; n.lan = up_lan();
    b = policy_select(&cfg, &n, TASK_COMPLEX_NLU, &r);
    ok(b == BE_LOCAL_MODEL && r == REASON_WAN_LOSS_HIGH,
       "复杂 NLU + WAN 丢包超阈 → 网关本地模型");

    /* 三条都超阈时的原因优先级（文档化为 时延 → 抖动 → 丢包） */
    n.wan = perfect_wan();
    n.wan.rtt_ms = 900; n.wan.jitter_ms = 200; n.wan.loss_pct = 30;
    n.lan = up_lan();
    b = policy_select(&cfg, &n, TASK_COMPLEX_NLU, &r);
    ok(b == BE_LOCAL_MODEL && r == REASON_WAN_RTT_HIGH,
       "多项超阈时按 时延→抖动→丢包 的顺序报告首要原因");

    /* --- 最深的降级：LAN 也断 --- */
    n.wan = perfect_wan(); n.lan = up_lan(); n.lan.reachable = 0;
    b = policy_select(&cfg, &n, TASK_COMPLEX_NLU, &r);
    ok(b == BE_LOCAL_RULES && r == REASON_LAN_DOWN,
       "复杂 NLU + WAN 良好但 LAN 断开 → 设备内规则（能力降级）");

    /* --- 消融开关 --- */
    cfg.safety_local_always = 0;
    n.wan = perfect_wan(); n.lan = up_lan();
    b = policy_select(&cfg, &n, TASK_SAFETY_CRITICAL, &r);
    ok(b == BE_CLOUD_MODEL && r == REASON_WAN_OK,
       "safety_local_always=0 时安全任务随链路判据（消融用）");

    cfg.safety_local_always = 0;
    n.wan = perfect_wan(); n.lan.rtt_ms = 5;
    n.wan.rtt_ms = 900;
    b = policy_select(&cfg, &n, TASK_SAFETY_CRITICAL, &r);
    ok(b == BE_LOCAL_MODEL,
       "消融下安全任务同样会降到网关模型（量化专用切片缺失的代价）");
}

/* ================================================================== */
/* 组 5：边界与鲁棒性                                                  */
/* ================================================================== */
static void test_robustness(void)
{
    policy_cfg_t cfg;
    net_state_t  n;
    policy_reason_t r;
    int i, seen_cloud_again = 0, entered_local = 0;

    policy_cfg_default(&cfg);
    n.lan = up_lan();

    /* 单调性：RTT 递增时，复杂任务的档位只能向"更本地"走，不能回摆 */
    for (i = 0; i <= 1200; i += 7) {
        backend_t b;
        n.wan = perfect_wan();
        n.wan.rtt_ms = i;
        b = policy_select(&cfg, &n, TASK_COMPLEX_NLU, &r);
        if (b != BE_CLOUD_MODEL) entered_local = 1;
        if (entered_local && b == BE_CLOUD_MODEL) seen_cloud_again = 1;
    }
    ok(!seen_cloud_again,
       "RTT 单调上升时档位单调不回升（无迟滞振荡）");

    /* cfg 传 NULL 用默认值 */
    n.wan = perfect_wan();
    ok(policy_select(NULL, &n, TASK_COMPLEX_NLU, &r) == BE_CLOUD_MODEL,
       "cfg=NULL 时使用默认阈值");

    /* net 传 NULL 不得崩溃，并走最保守分支 */
    ok(policy_select(&cfg, NULL, TASK_COMPLEX_NLU, &r) == BE_LOCAL_RULES,
       "net=NULL 时不崩溃并给出最保守结果");

    /* reason 可传 NULL */
    n.wan = perfect_wan();
    ok(policy_select(&cfg, &n, TASK_COMPLEX_NLU, NULL) == BE_CLOUD_MODEL,
       "reason=NULL 时不做原因回填");

    /* 未知任务类别退化为最简单分支 */
    n.wan = perfect_wan();
    ok(policy_select(&cfg, &n, (task_class_t)99, &r) == BE_LOCAL_RULES,
       "未知任务类别退化为本机规则（fail-safe）");
}

/* ================================================================== */
/* 组 6：名字表与日志渲染                                              */
/* ================================================================== */
static void test_names_and_log(void)
{
    policy_cfg_t cfg;
    net_state_t  n;
    char buf[512];
    int  len;

    policy_cfg_default(&cfg);
    n.wan = perfect_wan();
    n.lan = up_lan();

    ok(backend_name(BE_CLOUD_MODEL) && backend_name(BE_LOCAL_MODEL) &&
       backend_name(BE_LOCAL_RULES),
       "后端名表非空");
    ok(strcmp(backend_name(BE_CLOUD_MODEL), backend_name(BE_LOCAL_MODEL)) != 0 &&
       strcmp(backend_name(BE_LOCAL_MODEL), backend_name(BE_LOCAL_RULES)) != 0,
       "三个后端名互不相同");

    ok(policy_reason_name(REASON_WAN_OK) &&
       strcmp(policy_reason_name(REASON_WAN_OK), "WAN_OK") == 0,
       "原因名表可用");

    ok(task_class_name(TASK_COMPLEX_NLU) &&
       strcmp(task_class_name(TASK_COMPLEX_NLU), "complex-nlu") == 0,
       "任务类名表可用");

    len = policy_explain(buf, sizeof buf, &cfg, &n, TASK_COMPLEX_NLU);
    ok(len > 0 && (int)strlen(buf) == len, "policy_explain 返回准确长度");
    ok(strstr(buf, "cloud-model") != NULL, "日志含所选后端名");
    ok(strstr(buf, "WAN_OK") != NULL, "日志含决策原因");
    ok(strstr(buf, "complex-nlu") != NULL, "日志含任务类别");

    /* 截断安全：小缓冲区不得越界，且始终以 NUL 结尾 */
    {
        char small[16];
        int  i;
        for (i = 0; i < (int)sizeof small; i++) small[i] = (char)0x7F;
        small[sizeof small - 1] = '\0';
        len = policy_explain(small, 8, &cfg, &n, TASK_COMPLEX_NLU);
        ok(small[7] == '\0', "policy_explain 截断时仍在 buflen-1 处补 NUL");
        ok(len >= 7, "policy_explain 截断时返回所需长度而非缓冲区长度");
        ok(small[8] == (char)0x7F && small[9] == (char)0x7F,
           "policy_explain 未越界写入 buflen 之外");
    }

    ok(policy_explain(NULL, 16, &cfg, &n, TASK_COMPLEX_NLU) == -1,
       "policy_explain(NULL) 返回 -1");
    ok(policy_explain(buf, 0, &cfg, &n, TASK_COMPLEX_NLU) == -1,
       "policy_explain(buflen=0) 返回 -1");
}

/* ================================================================== */
/* 组 7：端到端叙事 —— "有台阶的降级"                                   */
/* ================================================================== */
static void test_graceful_degradation_story(void)
{
    policy_cfg_t cfg;
    net_state_t  n;
    backend_t    b1, b2, b3, b4;
    policy_reason_t r;

    policy_cfg_default(&cfg);
    n.lan = up_lan();

    /* 阶段 1：链路良好 */
    n.wan = perfect_wan();
    b1 = policy_select(&cfg, &n, TASK_COMPLEX_NLU, &r);

    /* 阶段 2：WAN 时延抬升 */
    n.wan.rtt_ms = 600;
    b2 = policy_select(&cfg, &n, TASK_COMPLEX_NLU, &r);

    /* 阶段 3：WAN 断开，LAN 尚存 */
    n.wan.reachable = 0;
    b3 = policy_select(&cfg, &n, TASK_COMPLEX_NLU, &r);

    /* 阶段 4：LAN 也断开 */
    n.lan.reachable = 0;
    b4 = policy_select(&cfg, &n, TASK_COMPLEX_NLU, &r);

    ok(b1 == BE_CLOUD_MODEL && b2 == BE_LOCAL_MODEL &&
       b3 == BE_LOCAL_MODEL && b4 == BE_LOCAL_RULES,
       "四级降级链完整：云 → 网关 → 网关 → 设备内");

    /* 关键论点：任何时候都有可用后端，不存在"无服务"状态 */
    ok(b1 > BE_NONE && b2 > BE_NONE && b3 > BE_NONE && b4 > BE_NONE,
       "任一链路状态下都有非 NONE 的后端可用（可用性无断点）");

    /* 安全关键任务在同样四级链路下档位恒定 */
    {
        backend_t s1, s2, s3, s4;
        net_state_t m;
        m.wan = perfect_wan(); m.lan = up_lan();
        s1 = policy_select(&cfg, &m, TASK_SAFETY_CRITICAL, NULL);
        m.wan.rtt_ms = 600;
        s2 = policy_select(&cfg, &m, TASK_SAFETY_CRITICAL, NULL);
        m.wan.reachable = 0;
        s3 = policy_select(&cfg, &m, TASK_SAFETY_CRITICAL, NULL);
        m.lan.reachable = 0;
        s4 = policy_select(&cfg, &m, TASK_SAFETY_CRITICAL, NULL);
        ok(s1 == s2 && s2 == s3 && s3 == s4 && s1 == BE_LOCAL_RULES,
           "安全关键任务档位对链路状态完全不敏感（专用切片论点）");
    }
}

/* ================================================================== */
int main(void)
{
    printf("=== offload_policy 单元测试 ===\n\n");

    test_cfg_and_init();
    test_link_update();
    test_jitter_estimator();
    test_budget();
    test_decision_table();
    test_robustness();
    test_names_and_log();
    test_graceful_degradation_story();

    printf("\n=== 汇总: %d passed, %d failed ===\n", g_pass, g_fail);
    return g_fail == 0 ? 0 : 1;
}
