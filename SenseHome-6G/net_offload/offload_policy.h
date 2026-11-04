/*
 * offload_policy.h — 链路感知的推理卸载策略
 *
 * 目标：把 openvela 智能家居 Agent 的 LLM 后端选择
 *       从「编译期由人选定、重编译才生效」（switch_llm.sh 现状）
 *       改为「运行期按链路状态与任务类别自动决策」。
 *
 * 为什么是两段链路：
 *   Agent 跑在设备侧（QEMU/ MCU）。三个后端相对设备的距离并不一样：
 *     - DeepSeek / 通义千问：经 WAN（6G/5G）到公网云
 *     - Ollama：经 LAN 到家庭网关/本地主机
 *     - 本地规则：设备内部，零网络
 *   把 WAN 与 LAN 压成一条链路，会掩盖「WAN 断了但 LAN 还在」这一最常见、
 *   也最有价值的中间态 —— 那正是本项目要证明的「有台阶的降级」。
 *
 * 可移植性：纯 C99，无动态内存、无浮点、无系统调用依赖，
 *           可直接编入 openvela Agent 核心（对标 32KB 核心栈约束）。
 */
#ifndef OFFLOAD_POLICY_H
#define OFFLOAD_POLICY_H

#define OFFLOAD_EWM_SHIFT 3   /* 指数加权平滑 α = 1/2^3 = 1/8 */
#define OFFLOAD_COMPUTE_QUEUE_MAX_MS 300
#define OFFLOAD_COMPUTE_INFLIGHT_MAX 8
#define OFFLOAD_COMPUTE_FREE_MEM_MIN_MB 256

/* ============ 推理后端档位（按能力递增、按可依赖性递减） ============ */
typedef enum {
    BE_NONE = 0,        /* 无可用后端（不应出现，出现即 bug） */
    BE_LOCAL_RULES,     /* 设备内规则直连：无 NLU，零网络，永远可用 */
    BE_LOCAL_MODEL,     /* 网关本地小模型（Ollama）：有 NLU，只依赖 LAN */
    BE_CLOUD_MODEL      /* 云端大模型（DeepSeek/通义）：NLU 最强，依赖 WAN */
} backend_t;

/* ============ 任务类别 ============ */
typedef enum {
    TASK_SIMPLE_CONTROL = 0,  /* 结构化指令："打开客厅的灯" —— 规则即可，无需大模型 */
    TASK_COMPLEX_NLU,         /* 模糊/多意图："有点冷"、"太暗了" —— 需要 NLU */
    TASK_SAFETY_CRITICAL      /* 安全关键：安防布防、燃气联动 —— 不得等网络 */
} task_class_t;

/* ============ 决策原因（写进决策日志，供评审与复盘） ============ */
typedef enum {
    REASON_WAN_OK = 0,          /* 上云链路达标 */
    REASON_WAN_UNKNOWN,         /* 尚未采样，冷启动保守处理 */
    REASON_WAN_UNREACHABLE,     /* WAN 不可达 */
    REASON_WAN_RTT_HIGH,        /* WAN 时延超阈 */
    REASON_WAN_JITTER_HIGH,     /* WAN 抖动超阈 */
    REASON_WAN_LOSS_HIGH,       /* WAN 丢包超阈 */
    REASON_LAN_DOWN,            /* LAN 也不可用，只能退到设备内 */
    REASON_TASK_IS_SIMPLE,      /* 任务本身不需要大模型 */
    REASON_TASK_IS_SAFETY,      /* 安全关键，强制设备内 */
    REASON_MODEL_UNAVAILABLE,    /* 目标模型进程未就绪 */
    REASON_COMPUTE_OVERLOADED    /* 队列、显存或并发超过运行预算 */
} policy_reason_t;

/* 后端健康观测。字段来自运行时，不包含模型内部或隐藏赛题参数。
 * 每个字段的 -1 明确表示未知；未知指标不参与资源阈值判定。
 * model_ready 取 -1/0/1，其余字段取 -1 或非负观测值。 */
typedef struct {
    int model_ready;
    int queue_ms;
    int free_mem_mb;
    int inflight;
} compute_state_t;

/* ============ 单条链路的状态快照（平滑量） ============ */
typedef struct {
    int reachable;    /* 1=最近一次探测成功 */
    int samples;      /* 累计采样次数；0 = 链路状态未知 */
    int rtt_ms;       /* 平滑往返时延（毫秒） */
    /*
     * 平滑抖动：相邻两次**原始样本**之差的平滑值（RFC 3550 式）。
     *
     * ⚠️ 刻意不用 |本次样本 − 平滑值|。那样一次台阶式跳变（例如断链恢复）
     *    会被当成"抖动"，数值可达数百毫秒，再花几十个样本才衰减掉 ——
     *    实测后果是链路早已恢复，策略却长时间出不来降级档。
     *    抖动要测的是"相邻样本之间的变化"，不是"离均值有多远"。
     */
    int jitter_ms;
    int loss_pct;     /* 平滑丢包率 0-100 */
    int last_rtt;     /* 上一次的原始 RTT 样本（供抖动计算） */
    int has_last;     /* last_rtt 是否有效（手工构造的状态可能没有历史） */
} link_state_t;

/* ============ 网络全景：两段链路 ============ */
typedef struct {
    link_state_t wan;   /* 设备 →（6G/5G）→ 云端 */
    link_state_t lan;   /* 设备 →（WiFi/以太）→ 家庭网关 Ollama 主机 */
} net_state_t;

/* ============ 策略阈值（可调，用于灵敏度分析） ============ */
typedef struct {
    int cloud_rtt_max_ms;     /* 允许上云的最大 RTT */
    int cloud_jitter_max_ms;  /* 允许上云的最大抖动 */
    int cloud_loss_max_pct;   /* 允许上云的最大丢包率 */
    int safety_local_always;  /* 安全关键是否强制设备内（1=强制；0=仅作消融） */
} policy_cfg_t;

/* ============ 接口 ============ */

/* 默认阈值。取值依据见 README「阈值取值依据」——注意它们是设计参数，
 * 不是哪份标准里的强制值，测量脚本会对它们做扫描。 */
void policy_cfg_default(policy_cfg_t *cfg);

/* 链路状态初始化（未知态） */
void link_state_init(link_state_t *st);

/* 网络全景初始化 */
void net_state_init(net_state_t *net);

void compute_state_init(compute_state_t *state);
/* 部分遥测入口使用未知初始化，不伪造显存/队列等未报告数值。 */
void compute_state_unknown(compute_state_t *state);

/*
 * 用一次真实探测结果更新链路状态（指数加权平滑，α=1/8）。
 *   ok      : 本次探测是否成功
 *   rtt_ms  : 本次往返时延；失败时忽略
 * 返回更新后的 samples。
 */
int link_update(link_state_t *st, int ok, int rtt_ms);

/* 链路是否已有可用估计（采过样且可达） */
int link_is_usable(const link_state_t *st);

/* 链路是否满足上云预算（时延/抖动/丢包三项全达标） */
int link_meets_budget(const policy_cfg_t *cfg, const link_state_t *link);

/*
 * 核心决策：给定网络状态与任务类别，选择推理后端。
 * cfg 传 NULL 用默认阈值；reason 可传 NULL。
 *
 * 决策表（7 行，README 与方案文档同表）：
 *   SAFETY      *        *      → LOCAL_RULES   TASK_IS_SAFETY
 *   SIMPLE      *        *      → LOCAL_RULES   TASK_IS_SIMPLE
 *   COMPLEX     未知     up     → LOCAL_MODEL   WAN_UNKNOWN
 *   COMPLEX     达标     up     → CLOUD_MODEL   WAN_OK
 *   COMPLEX     超阈     up     → LOCAL_MODEL   WAN_{RTT,JITTER,LOSS}_HIGH
 *   COMPLEX     不可达   up     → LOCAL_MODEL   WAN_UNREACHABLE
 *   COMPLEX     *        down   → LOCAL_RULES   LAN_DOWN
 */
backend_t policy_select(const policy_cfg_t *cfg,
                        const net_state_t *net,
                        task_class_t task,
                        policy_reason_t *reason);

backend_t policy_select_with_compute(const policy_cfg_t *cfg,
                                     const net_state_t *net,
                                     const compute_state_t *compute,
                                     task_class_t task,
                                     policy_reason_t *reason);

/* 名字表（日志/报告用；返回值恒非 NULL） */
const char *backend_name(backend_t b);
const char *task_class_name(task_class_t t);
const char *policy_reason_name(policy_reason_t r);

/*
 * 把一次决策渲染成一行人类可读日志，便于答辩现场展示"为什么这么选"。
 * 返回写入的字符数（不含结尾 NUL）；buf 太小则截断并返回截断长度，
 * 绝不越界写。buf 为 NULL 或 buflen<=0 时返回 -1。
 */
int policy_explain(char *buf, int buflen,
                   const policy_cfg_t *cfg,
                   const net_state_t *net,
                   task_class_t task);

#endif /* OFFLOAD_POLICY_H */
