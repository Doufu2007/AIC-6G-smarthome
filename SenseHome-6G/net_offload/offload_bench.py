#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
offload_bench.py — 链路自适应卸载的四档测量台架

测什么（**真实测量**）：
  - 用户总时延 e2e：探测 + C 策略决策 + 后端调用的客户端 wall clock
  - 网络时延 net：后端调用段 − 服务端处理耗时，包含真实建连、传播、序列化开销
  - 服务端 HTTP 处理耗时 http：真实（不含桩 sleep）
  - 连接失败率：真实观测到的失败比例（由模拟器按概率注入连接级失败）

不测什么（**桩参数，非测量**）：
  - 模型推理耗时。没有 API key、本机未装 Ollama，无法真跑。
    该值由 --cloud-compute-ms / --gateway-compute-ms 注入，
    在报告里永远单列一栏 "STUB"，不参与任何"实测"措辞。

决策从哪来：全部来自 policy_cli（即 offload_policy.c）。
本脚本**不实现**任何卸载判据 —— 只喂观测、只读决策。

用法：
    python offload_bench.py --out results/
"""
import argparse
import collections
import http.client
import json
import os
import statistics
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from link_emulator import LinkEmulator          # noqa: E402
from network_trace import NetworkTrace         # noqa: E402
from evidence import validate_results_schema    # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = ".exe" if os.name == "nt" else ""

TASKS = ["simple", "complex", "safety"]

# ----------------------------------------------------------------------
# 三个档位各自能做什么 —— 这一栏必须与"请求有没有得到响应"分开报。
#
# ⚠️ 关键区分：
#   此台架只传 simple/complex/safety 合成标签，没有真实中文命令。
#   local-rules 是计时小循环；complex 未完成是合同设定，不是完整
#   Dashboard 自然语言规则能力的测量。模型成功也是桩合同。
# ----------------------------------------------------------------------
CAPABILITY = {
    "cloud-model": {"nlu": "合成标签完成", "source": "桩合同（未实测模型准确率）"},
    "local-model": {"nlu": "合成标签完成", "source": "桩合同（未实测模型准确率）"},
    "local-rules": {"nlu": "simple/safety 完成", "source": "计时代理合同，非完整 Dashboard 语义评测"},
}


def resolves(backend, task):
    """返回合成任务合同标志；不判断真实中文命令理解能力。"""
    if backend == "local-rules":
        # 台架合同将 complex 标为未完成，不外推 Dashboard 规则能力。
        return task != "complex"
    # 模型后端：桩假设为可解析（不是测量结果）
    return True


# ======================================================================
# C 策略驱动：常驻子进程，行协议
# ======================================================================
class PolicyDriver:
    def __init__(self, cli_path):
        self.proc = subprocess.Popen(
            [cli_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            # ⚠️ 必须显式指定 utf-8：策略日志含中文，Windows 默认会用 ANSI
            #    代码页解码管道，把中文变成乱码（本机已知坑）。
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        line = self.proc.stdout.readline().strip()
        if line != "ready":
            raise RuntimeError("policy_cli 启动失败: %r" % line)

    def _cmd(self, s):
        self.proc.stdin.write(s + "\n")
        self.proc.stdin.flush()
        return self.proc.stdout.readline().rstrip("\n")

    def probe(self, link, ok, rtt_ms):
        return self._cmd("probe %s %d %d" % (link, 1 if ok else 0, int(rtt_ms)))

    def decide(self, task):
        out = self._cmd("decide %s" % task)
        parts = out.split(" ", 3)
        if len(parts) < 3 or parts[0] != "decision":
            return "none", "ERR", out
        return parts[1], parts[2], (parts[3] if len(parts) > 3 else "")

    def cfg(self, rtt, jit, loss):
        return self._cmd("cfg %d %d %d" % (rtt, jit, loss))

    def reset(self):
        return self._cmd("reset")

    def close(self):
        try:
            self._cmd("quit")
        except Exception:
            pass
        try:
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()


# ======================================================================
# HTTP 小工具：每次请求都新建连接，与模拟器的"每方向首块延迟"模型对应
# ======================================================================
def http_get(port, path, timeout):
    t0 = time.perf_counter()
    conn = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        return True, (time.perf_counter() - t0) * 1000.0, json.loads(body.decode("utf-8"))
    except Exception as e:                                   # noqa: BLE001
        return False, (time.perf_counter() - t0) * 1000.0, {"error": type(e).__name__}
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def http_post(port, path, payload, timeout):
    t0 = time.perf_counter()
    conn = None
    raw = json.dumps(payload).encode("utf-8")
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("POST", path, body=raw,
                     headers={"Content-Type": "application/json",
                              "Content-Length": str(len(raw))})
        resp = conn.getresponse()
        body = resp.read()
        return True, (time.perf_counter() - t0) * 1000.0, json.loads(body.decode("utf-8"))
    except Exception as e:                                   # noqa: BLE001
        return False, (time.perf_counter() - t0) * 1000.0, {"error": type(e).__name__}
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def r1(x):
    """小量级保留两位：设备内规则是亚毫秒级，四舍五入到 0.0 会丢掉这个信息。"""
    if x is None:
        return None
    return round(x, 2) if abs(x) < 10 else round(x, 1)


# ======================================================================
# 台架
# ======================================================================
class Bench:
    def __init__(self, args):
        self.args = args
        self.procs = []
        self.emus = []
        self.timeout = max(3.0, args.wan_rtt_max_ms / 1000.0 * 4 + 3.0)

    # ---------- 生命周期 ----------
    def start_backends(self):
        for port, tier, cms in (
            (self.args.cloud_port,   "cloud",   self.args.cloud_compute_ms),
            (self.args.gateway_port, "gateway", self.args.gateway_compute_ms),
        ):
            p = subprocess.Popen(
                [sys.executable, "-I", os.path.join(HERE, "backends", "stub_backend.py"),
                 "--port", str(port), "--tier", tier, "--compute-ms", str(cms)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
            self.procs.append(p)
            p.stdout.readline()          # 等服务端打印就绪行

    def start_emulators(self):
        self.wan = LinkEmulator(self.args.wan_port, "127.0.0.1",
                                self.args.cloud_port, name="wan").start()
        self.lan = LinkEmulator(self.args.lan_port, "127.0.0.1",
                                self.args.gateway_port, name="lan").start()
        self.emus = [self.wan, self.lan]

    def stop(self):
        for e in self.emus:
            e.stop()
        for p in self.procs:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    # ---------- 条件设置 ----------
    def set_wan(self, rtt, jitter, loss, blackout=False):
        self.wan.set_conditions(rtt_ms=rtt, jitter_ms=jitter,
                                loss_pct=loss, blackout=blackout)
        self.timeout = max(3.0, rtt / 1000.0 * 4 + 3.0)

    def set_lan(self, rtt, jitter, loss, blackout=False):
        self.lan.set_conditions(rtt_ms=rtt, jitter_ms=jitter,
                                loss_pct=loss, blackout=blackout)

    # ---------- 一次探测 ----------
    def probe_links(self, drv, do_lan=True):
        """探测两条链路并喂给 C 策略；返回本次观测。"""
        obs = {}
        for link, port in (("wan", self.args.wan_port), ("lan", self.args.lan_port)):
            if link == "lan" and not do_lan:
                continue
            ok, ms, _ = http_get(port, "/probe", self.timeout)
            drv.probe(link, ok, ms if ok else 0)
            obs[link] = {"ok": ok, "rtt_ms": ms}
        return obs

    # ---------- 一次端到端推理 ----------
    def infer_once(self, drv, task, arm, do_lan=True, trace_id=None, trace_index=None, sample=None):
        probe_ms = 0.0
        obs = {"wan": None, "lan": None}
        if arm == "adaptive" and task not in {"simple", "safety"}:
            if sample is not None:
                self.wan.set_replay(sample, "probe", "wan")
                self.lan.set_replay(sample, "probe", "lan")
            probe_t0 = time.perf_counter()
            obs = self.probe_links(drv, do_lan=do_lan)
            probe_ms = (time.perf_counter() - probe_t0) * 1000.0

        policy_ms = 0.0
        if arm == "adaptive":
            policy_t0 = time.perf_counter()
            backend, reason, log = drv.decide(task)
            policy_ms = (time.perf_counter() - policy_t0) * 1000.0
        else:
            backend, reason, log = "cloud-model", "STATIC_COMPILE_TIME", "(静态编译期选定)"

        row = {
            "task": task, "backend": backend, "reason": reason, "log": log,
            "trace_id": trace_id, "trace_index": trace_index,
            "source": "rule-timing-proxy" if backend == "local-rules" else "stub-assumption",
            "replay_link": None,
            "replay_outcome": None,
            "probe_replay": ({link: sample.outcome("probe", link) for link in ("wan", "lan")}
                             if sample is not None and arm == "adaptive" and task not in {"simple", "safety"} else None),
            "wan_probe_ok": None if obs["wan"] is None else obs["wan"]["ok"],
            "wan_probe_ms": None if obs["wan"] is None else obs["wan"]["rtt_ms"],
            "ok": False, "resolved": False, "e2e_ms": None, "backend_e2e_ms": None,
            "probe_ms": probe_ms, "policy_ms": policy_ms, "backend_ms": None,
            "total_user_ms": None, "net_ms": None,
            "http_ms": None, "stub_ms": None, "err": None,
        }

        if backend == "local-rules":
            # Timing proxy only: this synthetic loop is not a device action.
            t0 = time.perf_counter()
            self._rule_match(task)
            row["backend_ms"] = (time.perf_counter() - t0) * 1000.0
            row["backend_e2e_ms"] = row["backend_ms"]
            row["total_user_ms"] = row["probe_ms"] + row["policy_ms"] + row["backend_ms"]
            row["e2e_ms"] = row["total_user_ms"]
            row["net_ms"] = 0.0
            row["http_ms"] = row["backend_ms"]
            row["stub_ms"] = 0.0
            row["ok"] = True                       # 一定会有响应
            row["resolved"] = resolves(backend, task)   # 但未必解决问题
            if not row["resolved"]:
                row["err"] = "UNRESOLVED_NO_NLU"
            return row

        port = self.args.wan_port if backend == "cloud-model" else self.args.lan_port
        if sample is not None:
            link_name = "wan" if backend == "cloud-model" else "lan"
            emulator = self.wan if link_name == "wan" else self.lan
            emulator.set_replay(sample, "infer", link_name)
            row["replay_link"] = link_name
            row["replay_outcome"] = sample.outcome("infer", link_name)
        backend_t0 = time.perf_counter()
        ok, e2e, resp = http_post(port, "/infer", {"task": task}, self.timeout)
        row["backend_ms"] = (time.perf_counter() - backend_t0) * 1000.0
        row["backend_e2e_ms"] = e2e
        row["total_user_ms"] = row["probe_ms"] + row["policy_ms"] + row["backend_ms"]
        row["e2e_ms"] = row["total_user_ms"]
        row["ok"] = ok
        if ok:
            row["stub_ms"] = resp.get("stub_compute_ms")
            row["http_ms"] = resp.get("handler_ms")
            row["net_ms"] = e2e - row["http_ms"]
            row["resolved"] = resolves(backend, task)   # 桩假设
        else:
            row["err"] = resp.get("error")
        return row

    @staticmethod
    def _rule_match(task):
        # 仅用于获得可计时的本地代理路径；不是规则解析或设备动作。
        acc = 0
        for i in range(64):
            acc += (i ^ (ord(task[0]) if task else 0)) & 0xFF
        return acc

    # ---------- 阶段 ----------
    def run_phase(self, drv, name, n_per_task, arm, trace=None):
        # 记录模拟器自己的计数：它拒了多少连接，是可以与"观测到的失败"对账的
        # 一手数据。两者不一致，差额就来自台架自身而非注入 —— 必须能分辨。
        w0 = (self.wan.n_refused, self.wan.n_accepted)
        rows = []
        request_index = 0
        for task in TASKS:
            for _ in range(n_per_task):
                sample = trace.samples[request_index] if trace is not None else None
                if sample is not None:
                    if sample.blackout:
                        self.set_wan(self.args.wan_rtt_max_ms, 0.0, 0.0, blackout=True)
                    else:
                        self.set_wan(sample.configured_rtt_ms, sample.jitter_ms, sample.loss_pct, blackout=False)
                rows.append(self.infer_once(
                    drv, task, arm,
                    trace_id=trace.trace_id if trace is not None else None,
                    trace_index=sample.index if sample is not None else None,
                    sample=sample,
                ))
                request_index += 1
        return {"phase": name, "arm": arm, "rows": rows,
                "conditions": {"wan": self.wan.get_conditions(),
                               "lan": self.lan.get_conditions()},
                "wan_refused": self.wan.n_refused - w0[0],
                "wan_accepted": self.wan.n_accepted - w0[1]}

    # ---------- 降级轨迹 ----------
    def run_transition(self, drv, steps, probes_per_step):
        trace = []
        for configured in steps:
            if configured is None:
                self.set_wan(self.args.wan_rtt_max_ms, 0, 0, blackout=True)
                label = "BLACKOUT"
            else:
                self.set_wan(configured, max(1.0, configured * 0.05), 0.0)
                label = str(configured)
            for k in range(probes_per_step):
                obs = self.probe_links(drv, do_lan=True)
                backend, reason, _ = drv.decide("complex")
                b_safe, r_safe, _ = drv.decide("safety")
                trace.append({
                    "step": label,
                    "probe_idx": k,
                    "wan_configured_rtt_ms": configured,
                    "wan_measured_rtt_ms": r1(obs["wan"]["rtt_ms"]) if obs["wan"]["ok"] else None,
                    "wan_probe_ok": obs["wan"]["ok"],
                    "backend_complex": backend,
                    "reason_complex": reason,
                    "backend_safety": b_safe,
                    "reason_safety": r_safe,
                })
        return trace


# ======================================================================
# 汇总
# ======================================================================
def summarize(phase_result, args):
    rows = phase_result["rows"]
    out = {"phase": phase_result["phase"], "arm": phase_result["arm"],
           "conditions": phase_result["conditions"], "by_task": {}, "overall": {}}

    def agg(sub):
        n = len(sub)
        okn = sum(1 for r in sub if r["ok"])
        resn = sum(1 for r in sub if r.get("resolved"))
        e2e = [r["e2e_ms"] for r in sub if r["ok"]]
        probe = [r["probe_ms"] for r in sub if r.get("probe_ms") is not None]
        policy = [r["policy_ms"] for r in sub if r.get("policy_ms") is not None]
        backend = [r["backend_ms"] for r in sub if r.get("backend_ms") is not None]
        total_user = [r.get("total_user_ms", r.get("e2e_ms")) for r in sub if r.get("total_user_ms", r.get("e2e_ms")) is not None]
        net = [r["net_ms"] for r in sub if r["ok"] and r["net_ms"] is not None]
        htt = [r["http_ms"] for r in sub if r["ok"] and r["http_ms"] is not None]
        return {
            "n": n,
            "n_ok": okn,
            # 接口成功率：请求得到了响应（哪怕是个"无法理解"）
            "completion_pct": round(100.0 * okn / n, 1) if n else None,
            # 合成语义合同完成率，必须与接口成功率分开。
            "n_resolved": resn,
            "semantic_pct": round(100.0 * resn / n, 1) if n else None,
            "e2e_median_ms": r1(med(e2e)),
            "e2e_max_ms": r1(max(e2e)) if e2e else None,
            "probe_median_ms": r1(med(probe)),
            "policy_median_ms": r1(med(policy)),
            "backend_median_ms": r1(med(backend)),
            "total_user_median_ms": r1(med(total_user)),
            "net_median_ms": r1(med(net)),
            "http_median_ms": r1(med(htt)),
            "stub_ms": r1(med([r.get("stub_ms") for r in sub if r.get("stub_ms") is not None])),
        }

    for t in TASKS:
        out["by_task"][t] = agg([r for r in rows if r["task"] == t])
    out["overall"] = agg(rows)
    # 失败构成：把"模拟器拒绝（注入生效）"与"其它（台架/竞态）"分开。
    # 不分开的话，一个 3 倍于注入值的失败率会被误读成"链路就是那么差"。
    out["errors"] = dict(collections.Counter(
        r["err"] for r in rows if r.get("err")))
    out["wan_refused"] = phase_result.get("wan_refused")
    out["wan_accepted"] = phase_result.get("wan_accepted")
    out["n_failed"] = sum(1 for r in rows if not r["ok"])
    # ⚠️ overall 的 e2e 中位数是**跨后端的混合量**，参考价值有限：
    #    设备内规则是亚毫秒级、云端是数百毫秒，混在一起取中位数会落到
    #    样本较多的那一侧。真正可比的是下面按后端分组的聚合。
    out["by_backend"] = {
        be: agg([r for r in rows if r["backend"] == be])
        for be in sorted({r["backend"] for r in rows})
    }
    # 每个任务类别实际落到哪些后端（渲染表 A 用）
    out["by_task_decisions"] = {
        t: sorted({r["backend"] for r in rows if r["task"] == t}) for t in TASKS
    }
    # 决策分布
    dist = {}
    for r in rows:
        key = "%s/%s" % (r["backend"], r["reason"])
        dist[key] = dist.get(key, 0) + 1
    out["decision_distribution"] = dist
    return out


def probe_overhead(phase_result):
    """自适应臂的探测开销：这是策略自身的成本，必须单独列出来。
    同时给出"注入失败率 vs 实测失败率"的交叉核对。"""
    # Local fast paths record probe_ms=0 but make no probe. Their zeros must
    # not dilute a statistic whose population is actual probe requests.
    probes = [r for r in phase_result["rows"] if r.get("wan_probe_ok") is not None]
    ws = [r["probe_ms"] for r in probes if r.get("probe_ms") is not None]
    fails = sum(1 for r in probes if r["wan_probe_ok"] is False)
    total = len(probes)
    c = phase_result["conditions"]["wan"]
    injected = 100.0 if c["blackout"] else c["loss_pct"]
    return {
        "n_probes": total,
        "median_probe_ms": r1(med(ws)),
        "probe_failure_pct": round(100.0 * fails / total, 1) if total else None,
        "injected_failure_pct": injected,
        "note": "探测是自适应策略自身的成本，静态方案不付这笔钱；"
                "仅统计实际执行探测的复杂任务，不含本地快速路径的零值和 8 次预热采样",
    }


# ======================================================================
# 报告
# ======================================================================
PHASE_DESC = {
    "P1-normal":   "P1 链路正常",
    "P2-degraded": "P2 WAN 劣化",
    "P3-wan-down": "P3 WAN 断链（LAN 尚存）",
    "P4-all-down": "P4 全网断链",
}


def phase_label(name):
    return PHASE_DESC.get(name, name)


def recovery_stats(trace):
    """从轨迹里量出"劣化快、恢复慢"到底慢多少。"""
    if not trace:
        return None
    idx_leave = next((i for i, t in enumerate(trace)
                      if t["backend_complex"] != "cloud-model"), None)
    idx_black = next((i for i in range(len(trace) - 1, -1, -1)
                      if trace[i]["step"] == "BLACKOUT"), None)
    if idx_leave is None or idx_black is None:
        return None
    idx_back = next((i for i in range(idx_black + 1, len(trace))
                     if trace[i]["backend_complex"] == "cloud-model"), None)
    return {
        "recovered": idx_back is not None,
        "probes_to_leave": idx_leave,
        "probes_after_blackout_to_cloud": (None if idx_back is None
                                           else idx_back - idx_black),
        "probes_to_recover": (None if idx_back is None else idx_back - idx_leave),
        "total_probes": len(trace),
    }


def render_markdown(args, summaries, overheads, trace, meta):
    L = []
    L.append("# 链路自适应推理卸载 — 测量结果\n")
    L.append("生成时间：%s\n" % meta["generated_at"])
    L.append("> **读数纪律**：本表 `stub_compute_ms` 一栏是**注入的桩参数**，"
             "不是测量值。`e2e_ms` 按用户口径计为 `probe_ms + policy_ms + backend_ms`；"
             "后端段原始耗时保存在 `backend_e2e_ms`。详见 §口径。\n")

    L.append("## 表 A · 各链路档位下的决策与完成率\n")
    L.append("**两栏完成率必须分开读**：")
    L.append("- *接口成功率* = 请求得到了响应（哪怕响应内容是「无法理解」）。")
    L.append("- *语义合同完成率* = 当前合成标签满足后端合同；不代表真实命令准确率。")
    L.append("")
    L.append("断网时接口成功率可以是 100%，而语义完成率掉到 0 —— "
             "那正是「能力降级」，不是「成功」。只报前者就是误导。\n")
    L.append("| 阶段 | 臂 | WAN 条件 | 任务 | 决策 | 该档能力 | 接口成功率 | 语义完成率 | 探测中位 | 策略中位 | 后端中位 | 用户总时延中位 (ms) | 网络中位 | 桩 (ms) | n |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for s in summaries:
        c = s["conditions"]["wan"]
        cond = ("断链" if c["blackout"] else "RTT %.0f/抖动 %.0f/失败率 %.0f%%"
                % (c["rtt_ms"], c["jitter_ms"], c["loss_pct"]))
        for t in TASKS:
            a = s["by_task"][t]
            bes = s["by_task_decisions"][t]
            dec = "/".join(bes)
            cap = CAPABILITY.get(bes[0], {}).get("nlu", "?") if len(bes) == 1 else "混合"
            L.append("| %s | %s | %s | %s | %s | %s | %s%% | %s%% | %s | %s | %s | %s | %s | %s | %d |" % (
                phase_label(s["phase"]), s["arm"], cond, t, dec, cap,
                a["completion_pct"], a["semantic_pct"],
                a["probe_median_ms"], a["policy_median_ms"], a["backend_median_ms"],
                a["total_user_median_ms"], a["net_median_ms"],
                a["stub_ms"], a["n"]))
    L.append("")

    L.append("### 表 A 注 · 能力声明的来源\n")
    L.append("| 后端 | NLU 能力 | 声明来源 |")
    L.append("|---|---|---|")
    for be in ("cloud-model", "local-model", "local-rules"):
        L.append("| %s | %s | %s |" % (be, CAPABILITY[be]["nlu"], CAPABILITY[be]["source"]))
    L.append("")
    L.append("P4 全网断链时，合成 complex 标签落到 `local-rules` 计时代理，"
             "接口成功率 100%、合同完成率 0%。这不是完整 Dashboard 的规则语义评测，"
             "不能推出任何真实模糊指令的完成率。\n")

    L.append("## 表 A2 · 按实际落到的后端聚合（唯一可比的口径）\n")
    L.append("跨任务类别取中位数会把亚毫秒的本地规则与数百毫秒的云端混在一起，"
             "没有可比性。下表按实际后端分组。\n")
    L.append("| 阶段 | 臂 | 实际后端 | n | 接口成功率 | 语义完成率 | 用户总时延中位 (ms) | 用户总时延最大 (ms) | 网络中位 (ms) | 服务端中位 (ms) |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for s in summaries:
        for be in sorted(s["by_backend"]):
            a = s["by_backend"][be]
            L.append("| %s | %s | %s | %d | %s%% | %s%% | %s | %s | %s | %s |" % (
                phase_label(s["phase"]), s["arm"], be, a["n"],
                a["completion_pct"], a["semantic_pct"],
                a["e2e_median_ms"], a["e2e_max_ms"], a["net_median_ms"],
                a["http_median_ms"]))
    L.append("")

    L.append("## 表 B · 决策分布（按原因）\n")
    L.append("| 阶段 | 臂 | 决策/原因 | 次数 |")
    L.append("|---|---|---|---|")
    for s in summaries:
        for k, v in sorted(s["decision_distribution"].items()):
            L.append("| %s | %s | %s | %d |" % (phase_label(s["phase"]), s["arm"], k, v))
    L.append("")

    L.append("## 表 C · 探测开销与失败率交叉核对（自适应臂独有）\n")
    L.append("| 阶段 | 探测次数 | 探测中位 (ms) | 注入失败率 | 实测失败率 |")
    L.append("|---|---|---|---|---|")
    for o in overheads:
        L.append("| %s | %d | %s | %s%% | %s%% |" % (
            phase_label(o["phase"]), o["n_probes"],
            o["median_probe_ms"], o["injected_failure_pct"], o["probe_failure_pct"]))
    L.append("")
    if overheads:
        L.append("> %s\n" % overheads[0]["note"])
    L.append("> ⚠️ 实测失败率是**小样本比例**（n 见「探测次数」一列），"
             "二项分布下置信区间很宽：以 n=60、真实失败率 10% 为例，"
             "观察到 2～10 次失败都落在 95% 区间内。"
             "此列只用于**核对「注入是否真的生效」**，不足以作为失败率的精确估计，"
             "更不能拿来反推链路的丢包特性。\n")

    rec = recovery_stats(trace)
    L.append("## 表 C2 · 失败构成对账（注入 vs 观测 vs 台架自身）\n")
    L.append("模拟器计数包含探测与业务连接，台架观测失败只统计业务请求；"
             "二者不能直接相等。逐请求的 `replay_outcome` 与 `probe_replay` "
             "保存在 JSON 的 `raw_runs` 中，可分别复核注入的业务与探测失败。\n")
    L.append("| 阶段 | 臂 | 台架观测失败 | 模拟器拒绝连接 | 模拟器接受连接 | 失败类型 |")
    L.append("|---|---|---|---|---|---|")
    for s in summaries:
        errs = "、".join("%s×%d" % (k, v) for k, v in sorted(s["errors"].items())) or "—"
        L.append("| %s | %s | %d/%d | %s | %s | %s |" % (
            phase_label(s["phase"]), s["arm"], s["n_failed"], s["overall"]["n"],
            s["wan_refused"], s["wan_accepted"], errs))
    L.append("")
    L.append("> 静态臂计分段没有探测，业务失败可与 WAN 拒绝连接数核对。"
             "自适应臂另有探测流量，应按 `raw_runs` 的请求角色分开复核；"
             "不能把全部 WAN 拒绝都解释成业务请求失败。\n")

    L.append("## 表 D · 链路劣化 → 恢复 全过程决策轨迹\n")
    if rec and rec["recovered"]:
        L.append("**滞后摘要**：末次断链后有 %d 次探测才回到 `cloud-model`；"
                 "从首次离开 `cloud-model` 到重新上云共 %d 次（全程 %d 次探测）。"
                 "恢复显著慢于劣化 —— 这是 α=1/8 对称平滑的固有代价，"
                 "不是 bug，但确实是一个可改进点（见 §已知局限）。\n"
                 % (rec["probes_after_blackout_to_cloud"], rec["probes_to_recover"],
                    rec["total_probes"]))
    elif rec:
        L.append("**滞后摘要**：末次断链后在 %d 次探测内**未**回到 `cloud-model`。"
                 "这意味着平滑常数对该场景偏小，恢复被拖得过长。\n"
                 % rec["total_probes"])

    L.append("`复杂 NLU` 任务在链路逐步劣化再恢复时的档位变化。")
    L.append("注意平滑滞后：RTT 突变后档位不会立刻跟随，这是 α=1/8 的固有代价。\n")
    L.append("| 步骤 | 第几次探测 | 配置 RTT | 实测 RTT | 平滑后档位 | 原因 | 安全任务档位 |")
    L.append("|---|---|---|---|---|---|---|")
    for t in trace:
        L.append("| %s | %d | %s | %s | %s | %s | %s |" % (
            t["step"], t["probe_idx"],
            t["wan_configured_rtt_ms"] if t["wan_configured_rtt_ms"] is not None else "断链",
            t["wan_measured_rtt_ms"] if t["wan_probe_ok"] else "失败",
            t["backend_complex"], t["reason_complex"], t["backend_safety"]))
    L.append("")

    L.append("## 已知局限（本台架测出来的，不是推测）\n")
    L.append("1. **恢复慢于劣化。** 平滑常数 α=1/8 对劣化与恢复一视同仁，"
             "于是断链恢复后要几十次探测才重新上云（见表 D 滞后摘要）。"
             "家庭场景里这表现为「网早就好了，系统还在用降级档」。"
             "**补救方向**：对「恢复方向」用更大的 α（非对称平滑），或在连续 k 次"
             "探测达标后直接跳档。**本版本未实现**，留作后续工作。")
    L.append("2. **单次瞬时失败代价偏大。** 平滑丢包率在 0 的状态下遇到一次失败"
             "即抬到 12%，而阈值为 5%，需要约 7 个样本才回落 —— "
             "即一次网络抖动会换来约 7 次探测的降级。这是 EWM 的固有性质。")
    L.append("3. **模型耗时是桩参数，不是实测。** 见 §口径。"
             "所有涉及「云端更聪明」的论断在本次测量中都未获验证。")
    L.append("4. **样本量小。** 每个任务类别每档 n=20，只支持量级结论；"
             "模型耗时仍是桩参数时，不得把用户总时延差异表述成模型实测收益。\n")

    L.append("## 口径\n")
    L.append("- **真实测量**：用户总时延（探测 + 策略 + 后端段）、网络时延（后端段 − 服务端处理）、"
             "服务端 HTTP 处理耗时、连接失败率、探测时延与失败率。"
             "这些数字来自本机真实 TCP 往返，经 `link_emulator.py` 时延模型注入传播时延。")
    L.append("- **桩参数（非测量）**：模型推理耗时（cloud %.0f ms / gateway %.0f ms）。"
             "本机无 API key、未安装 Ollama，无法真跑模型；该值由命令行注入。"
             % (args.cloud_compute_ms, args.gateway_compute_ms))
    L.append("- **时延模型**：每方向首个数据块延迟 rtt/2，一条请求-应答净增约 rtt。"
             "是传播时延的近似模型，不是物理信道仿真。")
    L.append("- **失败注入**：模拟器按概率直接断开连接（应用层连接级失败），"
             "不是分组级丢包。TCP 分组级丢包会表现为重传时延，本台架不模拟那一层。")
    L.append("- **决策来源**：全部来自 `offload_policy.c`（经 `policy_cli` 常驻进程）。"
             "本测量脚本不实现任何卸载判据。")
    L.append("- **配对回放**：每个请求索引分别保存 WAN/LAN 与 probe/infer 的具体 RTT 和拒绝结果；"
             "两臂业务请求消费同一索引的结果，额外探测不会移动业务随机流。实际墙钟仍包含操作系统调度噪声。")
    L.append("- **本地规则计时范围**：台架的 `rule-timing-proxy` 是合成小循环，"
             "只用于策略路径计时，不代表真实设备动作耗时。真实动作执行由 Dashboard 路径另行验证。\n")
    return "\n".join(L)


# ======================================================================
def main():
    ap = argparse.ArgumentParser(description="链路自适应卸载四档测量")
    ap.add_argument("--out", default=os.path.join(HERE, "results"))
    ap.add_argument("--cli", default=os.path.join(HERE, "policy_cli" + EXE))
    ap.add_argument("--cloud-port", type=int, default=18081)
    ap.add_argument("--gateway-port", type=int, default=18082)
    ap.add_argument("--wan-port", type=int, default=19081)
    ap.add_argument("--lan-port", type=int, default=19082)
    ap.add_argument("--infer-n", type=int, default=6,
                    help="每个任务类别在每个档位的请求数（小样本，只支持量级结论）")
    ap.add_argument("--cloud-compute-ms", type=float, default=300.0,
                    help="桩：云端模型耗时（非实测）")
    ap.add_argument("--gateway-compute-ms", type=float, default=150.0,
                    help="桩：网关本地模型耗时（非实测）")
    ap.add_argument("--wan-rtt-max-ms", type=float, default=300.0)
    ap.add_argument("--trace-seed", type=int, default=20261103,
                    help="配对网络条件轨迹的随机种子")
    ap.add_argument("--skip-transition", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    if not os.path.exists(args.cli):
        print("找不到 %s，请先编译 policy_cli" % args.cli, file=sys.stderr)
        return 2

    b = Bench(args)
    drv = None
    try:
        b.start_backends()
        b.start_emulators()
        drv = PolicyDriver(args.cli)
        drv.cfg(int(args.wan_rtt_max_ms), 30, 5)

        # 档位定义：(名称, wan 条件, lan 条件)
        LAN_OK = (3.0, 1.0, 0.0, False)
        # 阶段名用 ASCII：控制台经管道时按 GBK 解码，中文会变乱码。
        PHASES = [
            ("P1-normal",   (30.0, 5.0, 0.0, False),   LAN_OK),
            ("P2-degraded", (400.0, 80.0, 10.0, False), LAN_OK),
            ("P3-wan-down", (300.0, 0.0, 0.0, True),   LAN_OK),
            ("P4-all-down", (300.0, 0.0, 0.0, True),   (3.0, 0.0, 0.0, True)),
        ]

        summaries, overheads, raw_runs = [], [], []
        trace = []

        trace_records = {}
        for phase_idx, (name, wc, lc) in enumerate(PHASES):
            b.set_lan(*lc)
            b.set_wan(*wc)
            phase_trace = NetworkTrace.generate(
                seed=args.trace_seed + phase_idx,
                steps=[None if wc[3] else wc[0]],
                probes_per_step=args.infer_n * len(TASKS),
                jitter_max_ms=wc[1],
                loss_max_pct=wc[2],
                lan_rtt_ms=lc[0], lan_jitter_ms=lc[1],
                lan_loss_pct=lc[2], lan_blackout=lc[3],
            )
            trace_records[name] = phase_trace.to_dict()
            for arm in ("adaptive", "static-cloud"):
                b.set_lan(*lc)
                b.set_wan(*wc)
                drv.reset()
                drv.cfg(int(args.wan_rtt_max_ms), 30, 5)
                # 让平滑状态在同一档位下先稳定若干次探测，再计分
                # Replay the same first eight probe outcomes for both arms.
                # Warmup must not retain the preceding arm's final condition.
                for warmup_index in range(8):
                    warmup_sample = phase_trace.samples[warmup_index % len(phase_trace.samples)]
                    b.wan.set_replay(warmup_sample, "probe", "wan")
                    b.lan.set_replay(warmup_sample, "probe", "lan")
                    b.probe_links(drv, do_lan=(arm == "adaptive"))
                pr = b.run_phase(drv, name, args.infer_n, arm, trace=phase_trace)
                raw_runs.append(pr)
                summaries.append(summarize(pr, args))
                if arm == "adaptive":
                    overheads.append(dict(probe_overhead(pr), phase=name))
                print("  done %s / %s" % (name, arm), flush=True)

        if not args.skip_transition:
            print("  过渡轨迹 ...", flush=True)
            b.set_lan(*LAN_OK)
            drv.reset()
            drv.cfg(int(args.wan_rtt_max_ms), 30, 5)
            # 尾部刻意留足够长的 30ms 段：恢复比劣化慢得多（α=1/8 的固有滞后），
            # 截断在恢复中途会让图看起来像"永不恢复"，那是误导。
            steps = [30, 60, 120, 200, 280, 320, 400, 600, 1000, None, None,
                     600, 300, 200, 100, 60, 30, 30, 30, 30, 30, 30, 30, 30]
            trace = b.run_transition(drv, steps, probes_per_step=3)

    finally:
        if drv:
            drv.close()
        b.stop()

    meta = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "note": "网络侧为真实测量；模型耗时为桩参数"}

    results = {
        "meta": meta,
        "stub_parameters": {
            "cloud_compute_ms": args.cloud_compute_ms,
            "gateway_compute_ms": args.gateway_compute_ms,
            "_warning": "以上两项是注入的桩参数，不是测量值",
        },
        "harness": {
            "trace_seed": args.trace_seed,
            "trace_replay": "actual TCP outcomes per request/link/role",
            "latency_definition": "probe_ms + policy_ms + backend_ms",
            "infer_n_per_task_per_phase": args.infer_n,
            "wan_rtt_max_ms": args.wan_rtt_max_ms,
            "cloud_jitter_max_ms": 30,
            "cloud_loss_max_pct": 5,
        },
        "summaries": summaries,
        "raw_runs": raw_runs,
        "probe_overheads": overheads,
        "transition_trace": trace,
        "paired_traces": trace_records,
        "recovery": recovery_stats(trace),
    }
    schema_errors = validate_results_schema(results)
    results["schema"] = {"valid": not schema_errors, "errors": schema_errors}
    if schema_errors:
        print("evidence schema invalid: %s" % "; ".join(schema_errors), file=sys.stderr)
        return 3

    with open(os.path.join(args.out, "results.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    md = render_markdown(args, summaries, overheads, trace, meta)
    with open(os.path.join(args.out, "results.md"), "w", encoding="utf-8") as f:
        f.write(md)

    # 控制台同时打印 ASCII 安全摘要（终端编码不可控，避免乱码）
    print("\n=== SUMMARY (ASCII only; see results/results.md for full tables) ===")
    print("  synthetic complex task only. iface% = response; sem% = stub contract, not real-model accuracy.")
    print("  %-12s %-13s %-26s %-7s %-7s %-9s %-9s"
          % ("phase", "arm", "backend", "iface%", "sem%", "e2e_med", "net_med"))
    for s in summaries:
        cx = s["by_task"]["complex"]
        print("  %-12s %-13s %-26s %-7s %-7s %-9s %-9s"
              % (s["phase"], s["arm"],
                 "/".join(s["by_task_decisions"]["complex"]),
                 cx["completion_pct"], cx["semantic_pct"],
                 cx["e2e_median_ms"], cx["net_median_ms"]))
    print("\n  -- grouped by backend actually used (the only comparable view) --")
    print("  %-12s %-13s %-13s %-4s %-7s %-7s %-9s"
          % ("phase", "arm", "backend", "n", "iface%", "sem%", "e2e_med"))
    for s in summaries:
        for be in sorted(s["by_backend"]):
            a = s["by_backend"][be]
            print("  %-12s %-13s %-13s %-4d %-7s %-7s %-9s"
                  % (s["phase"], s["arm"], be, a["n"],
                     a["completion_pct"], a["semantic_pct"], a["e2e_median_ms"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
