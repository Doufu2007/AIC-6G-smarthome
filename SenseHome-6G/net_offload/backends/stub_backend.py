#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stub_backend.py — 推理后端桩（**不调用任何大模型**）

⚠️ 诚实声明（本文件的存在本身就是为了让这件事无法被含糊过去）：
   这不是 LLM。它不加载权重、不推理、不产生 token。它做两件事：
     GET  /probe  → 立即返回一个极小的报文（用于测真实 RTT）
     POST /infer  → 先 sleep 一段**由参数注入**的"模型耗时"，再返回固定应答
   sleep 的时长是桩参数，**不是测量结果**，报告里必须与网络实测值分开列。

   之所以要有它：卸载策略要生效，三个档位就必须能被真正路由到三个不同的
   网络位置。没有后端，就没有可路由的终点，策略测试会退化成纯函数测试。
   网络往返是真的，模型耗时是假的 —— 这条界线不能糊。

用法：
    python stub_backend.py --port 18081 --tier cloud --compute-ms 300
"""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARGS = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):   # 静音默认访问日志
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/probe"):
            # 探测响应刻意做到最小，让测量值尽量贴近纯传播时延
            self._send({"probe": True, "tier": ARGS.tier})
        else:
            self._send({"error": "not_found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n:
            self.rfile.read(n)

        t0 = time.perf_counter()
        # ↓↓↓ 这一步是桩：注入的"模型耗时"，不是被测系统的真实算力时延 ↓↓↓
        time.sleep(ARGS.compute_ms / 1000.0)
        handler_ms = (time.perf_counter() - t0) * 1000.0

        self._send({
            "ok": True,
            "tier": ARGS.tier,
            "stub_compute_ms": ARGS.compute_ms,   # 桩参数，报告里单列
            "handler_ms": handler_ms,             # 含桩 sleep 的服务端总耗时
        })


def main():
    global ARGS
    ap = argparse.ArgumentParser(description="推理后端桩（非真实模型）")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--tier", default="cloud", help="cloud / gateway")
    ap.add_argument("--compute-ms", type=float, default=300.0,
                    help="注入的模型耗时桩参数（非实测）")
    ARGS = ap.parse_args()

    srv = ThreadingHTTPServer(("127.0.0.1", ARGS.port), Handler)
    srv.daemon_threads = True
    print("stub backend: tier=%s port=%d compute_ms=%.1f (STUB)"
          % (ARGS.tier, ARGS.port, ARGS.compute_ms), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
