#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render the latest schema-validated evidence JSON as a readable report."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

PHASE_CN = {
    "P1-normal": "P1 链路正常",
    "P2-degraded": "P2 WAN 劣化",
    "P3-wan-down": "P3 WAN 断链（LAN 尚存）",
    "P4-all-down": "P4 全网断链",
}
TASK_CN = {"simple": "simple（结构化）", "complex": "complex（复杂）", "safety": "safety（安全）"}


def fmt(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def render(data: dict) -> str:
    harness = data.get("harness", {})
    stubs = data.get("stub_parameters", {})
    lines = [
        "# SenseHome 链路自适应推理卸载：最新四档链路验证结果", "",
        f"> 生成时间：{data.get('meta', {}).get('generated_at', 'unknown')}；每档每任务 n={harness.get('infer_n_per_task_per_phase', 'unknown')}。",
        "> 用户总时延 = 探测耗时 + 策略耗时 + 后端耗时；`backend_e2e_ms` 仅保留后端段原始耗时。",
        "> 网络与策略路径来自本机 TCP 台架实测；模型推理耗时是注入的桩参数，不能解释为真实 GPU/模型测量。",
        "> 语义字段来自合成任务合同；local-rules 的小循环仅为计时代理，不能代表完整 Dashboard 规则能力。",
        "> 本结果不声称真实 5G/6G 空口、真实家庭试点或网络切片已完成。", "",
        "## 桩参数与复现实验", "",
        "| 项目 | 值 | 状态 |", "|---|---:|---|",
        f"| cloud model compute | {fmt(stubs.get('cloud_compute_ms'))} ms | 注入桩参数 |",
        f"| gateway model compute | {fmt(stubs.get('gateway_compute_ms'))} ms | 注入桩参数 |",
        "| paired traces | 每档 adaptive/static 共用同一 trace_id | 可重放 |", "",
        "## 表 A：按任务的用户总时延与完成率", "",
        "| 档位 | 臂 | 任务 | 后端 | 接口成功率 | 语义合同完成率 | 探测中位 ms | 策略中位 ms | 后端中位 ms | 用户总时延中位 ms | n |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for summary in data.get("summaries", []):
        phase, arm = summary.get("phase"), summary.get("arm")
        for task, row in summary.get("by_task", {}).items():
            decisions = summary.get("by_task_decisions", {}).get(task, [])
            backend = decisions[0] if decisions else "—"
            lines.append(
                f"| {PHASE_CN.get(phase, phase)} | {arm} | {TASK_CN.get(task, task)} | {backend} | "
                f"{fmt(row.get('completion_pct'))}% | {fmt(row.get('semantic_pct'))}% | "
                f"{fmt(row.get('probe_median_ms'))} | {fmt(row.get('policy_median_ms'))} | "
                f"{fmt(row.get('backend_median_ms'))} | {fmt(row.get('total_user_median_ms'))} | {fmt(row.get('n'))} |"
            )
    lines += ["", "## 表 B：自适应复杂 NLU 的档位对比", "",
              "| 档位 | 后端 | 接口成功率 | 语义合同完成率 | 用户总时延中位 ms | 探测中位 ms | 后端中位 ms |",
              "|---|---|---:|---:|---:|---:|---:|"]
    for summary in data.get("summaries", []):
        if summary.get("arm") != "adaptive":
            continue
        row = summary.get("by_task", {}).get("complex", {})
        decisions = summary.get("by_task_decisions", {}).get("complex", [])
        lines.append(
            f"| {PHASE_CN.get(summary.get('phase'), summary.get('phase'))} | {decisions[0] if decisions else '—'} | "
            f"{fmt(row.get('completion_pct'))}% | {fmt(row.get('semantic_pct'))}% | "
            f"{fmt(row.get('total_user_median_ms'))} | {fmt(row.get('probe_median_ms'))} | {fmt(row.get('backend_median_ms'))} |"
        )
    lines += ["", "## 表 C：可追溯性与限制", "",
              "- 每个阶段的 adaptive/static-cloud 两臂共享 `paired_traces` 中的同一 `trace_id`，可按 seed 重放。",
              "- `interface_ok` 表示收到后端响应；当前 `semantic_complete` 由桩合同给出。全网断链时合成 complex 的接口仍可用，但合同完成率为 0。真实命令语义必须单独评测。",
              "- 当前最大额外成本是劣化档探测；后续优化方向是异步探测缓存和真实模型复测。",
              "- 真实模型、真实 GPU、真实 5G/6G 空口、真实家庭试点和网络切片均属于待验证项。", "",
              "## 复现命令", "", "```powershell",
              "powershell -NoProfile -ExecutionPolicy Bypass -File SenseHome-6G/net_offload/run_evidence.ps1 -Out _work/new_evidence_run -InferN 20 -TraceSeed 20261103",
              "python -m unittest discover -s SenseHome-6G/net_offload/tests -v", "```"]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(data), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
