#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
link_emulator.py — 可注入时延/抖动/丢包/断链的 TCP 链路模拟器

它在设备与推理后端之间做一次真实的 TCP 中转。经过它的每一次往返，
都是操作系统栈里真真切切的两次 send/recv；注入的时延是 sleep 出来的
传播时延模型。因此**这里测到的 RTT 是真实测量值**，不是查表得来的。

⚠️ 必须如实标注的两点：
 1. 时延模型 = 每方向首个数据块延迟 rtt/2（近似单向传播时延）。
    一条请求-应答的净增时延约等于 rtt。这是**模型**，不是物理信道仿真。
 2. 丢包 = **应用层连接级失败**（按概率直接断开连接），不是分组级丢包。
    TCP 分组级丢包会表现为重传时延，本模拟器不模拟那一层。
    所以报告里它叫 "连接失败率"，不叫 "丢包率"。

单独运行：
    python link_emulator.py --listen 19081 --upstream 127.0.0.1:18081 --rtt 400 --loss 0.1
"""
import argparse
import random
import socket
import threading
import time


class LinkEmulator:
    """监听本地端口，把流量转发到 upstream，途中注入链路损伤。"""

    def __init__(self, listen_port, upstream_host, upstream_port,
                 rtt_ms=0.0, jitter_ms=0.0, loss_pct=0.0, blackout=False,
                 name="link"):
        self.listen_port = listen_port
        self.upstream = (upstream_host, upstream_port)
        self.name = name

        self._lock = threading.Lock()
        self._rtt_ms = float(rtt_ms)
        self._jitter_ms = float(jitter_ms)
        self._loss_pct = float(loss_pct)
        self._blackout = bool(blackout)
        self._replay = None

        self._srv = None
        self._thread = None
        self._stop = threading.Event()

        # 计数（供报告交叉核对，不作为指标）
        self.n_accepted = 0
        self.n_refused = 0

    # ---------------- 运行期调节 ----------------
    def set_conditions(self, rtt_ms=None, jitter_ms=None, loss_pct=None,
                       blackout=None):
        with self._lock:
            self._replay = None
            if rtt_ms is not None:
                self._rtt_ms = float(rtt_ms)
            if jitter_ms is not None:
                self._jitter_ms = float(jitter_ms)
            if loss_pct is not None:
                self._loss_pct = float(loss_pct)
            if blackout is not None:
                self._blackout = bool(blackout)

    def set_replay(self, sample, role, link="wan"):
        """Freeze the next connection outcome from a generated trace sample.

        Reusing this sample for the other experimental arm produces the same
        refusal and propagation delay. Probe and inference outcomes use
        independent streams and do not consume one another's randomness.
        """
        outcome = sample.outcome(role, link)
        with self._lock:
            self._replay = (float(outcome["rtt_ms"]) / 2000.0, bool(outcome["refused"]))

    def _connection_outcome(self):
        # Capture at accept time so a later request cannot change the delay
        # of a connection whose response is still being transferred.
        with self._lock:
            replay = self._replay
        if replay is not None:
            delay, refused = replay
            return refused, delay
        return self._should_refuse(), None

    def get_conditions(self):
        with self._lock:
            return {
                "rtt_ms": self._rtt_ms,
                "jitter_ms": self._jitter_ms,
                "loss_pct": self._loss_pct,
                "blackout": self._blackout,
            }

    def _one_way_delay(self):
        """本次连接单向传播时延（秒）"""
        with self._lock:
            rtt = self._rtt_ms
            jit = self._jitter_ms
        if rtt <= 0:
            return 0.0
        jittered = rtt + (random.uniform(-jit, jit) if jit > 0 else 0.0)
        if jittered < 0:
            jittered = 0.0
        return jittered / 2000.0   # 单向 = RTT/2

    def _should_refuse(self):
        with self._lock:
            if self._blackout:
                return True
            return random.random() < (self._loss_pct / 100.0)

    # ---------------- 启动/停止 ----------------
    def start(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", self.listen_port))
        self._srv.listen(64)
        self._srv.settimeout(0.5)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._srv:
            try:
                self._srv.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                cli, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            refused, one_way_delay = self._connection_outcome()
            if refused:
                # 连接级拒绝：客户端立刻看到 EOF/Reset，对应"链路不可达"
                self.n_refused += 1
                try:
                    cli.close()
                except OSError:
                    pass
                continue

            self.n_accepted += 1
            threading.Thread(target=self._handle, args=(cli, one_way_delay), daemon=True).start()

    def _handle(self, cli, one_way_delay=None):
        up = None
        try:
            up = socket.create_connection(self.upstream, timeout=5.0)
        except OSError:
            try:
                cli.close()
            except OSError:
                pass
            return

        t1 = threading.Thread(target=self._pump, args=(cli, up, one_way_delay), daemon=True)
        t2 = threading.Thread(target=self._pump, args=(up, cli, one_way_delay), daemon=True)
        t1.start()
        t2.start()
        t1.join(timeout=30.0)
        t2.join(timeout=30.0)
        for s in (cli, up):
            try:
                s.close()
            except OSError:
                pass

    def _pump(self, src, dst, one_way_delay=None):
        """单向搬运；只对本方向的首个数据块施加传播时延。"""
        first = True
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                if first:
                    d = self._one_way_delay() if one_way_delay is None else one_way_delay
                    if d > 0:
                        time.sleep(d)
                    first = False
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser(description="TCP 链路模拟器")
    ap.add_argument("--listen", type=int, required=True)
    ap.add_argument("--upstream", required=True, help="host:port")
    ap.add_argument("--rtt", type=float, default=0.0)
    ap.add_argument("--jitter", type=float, default=0.0)
    ap.add_argument("--loss", type=float, default=0.0, help="连接失败率 %%")
    ap.add_argument("--blackout", action="store_true")
    args = ap.parse_args()

    host, _, port = args.upstream.partition(":")
    emu = LinkEmulator(args.listen, host, int(port),
                       rtt_ms=args.rtt, jitter_ms=args.jitter,
                       loss_pct=args.loss, blackout=args.blackout).start()
    print("link emulator: 127.0.0.1:%d -> %s  %s"
          % (args.listen, args.upstream, emu.get_conditions()))
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        emu.stop()


if __name__ == "__main__":
    main()
