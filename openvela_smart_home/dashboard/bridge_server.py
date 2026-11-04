#!/usr/bin/env python3
"""
bridge_server.py v2.0 — openvela 物理AI Web Dashboard 桥接服务器

物理AI核心特性：
  1. 模糊指令理解："有点冷"/"太暗了" → 感知环境 → 自主决策
  2. 主动环境巡检：定时触发，Agent自主感知+决策+执行，无需用户指令
  3. 多传感器模拟：光照/人体红外/门磁/PM2.5/CO2/噪音
  4. 多设备协同：灯光+窗帘+安防 联动编排（离家模式）
  5. 端侧推理演示：本地决策，不依赖云端

用法：
  python3 bridge_server.py [--port 8080] [--patrol-interval 60]
"""
import http.server
import json
import os
import sys
import time
import re
import random
import threading
import atexit
import tempfile
from html import escape as html_escape
from urllib.parse import urlparse
from backend_monitor import BackendMonitor
from device_actions import execute_device_actions

# ============ 配置 ============
PORT = 8080
PATROL_INTERVAL = 60  # 主动巡检间隔（秒），0 表示禁用
MAX_REQUEST_BODY_BYTES = 64 * 1024
MAX_COMMAND_CHARS = 4096
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
STATE_FILE = os.path.join(PROJECT_DIR, "device_state.json")
NET_OFFLOAD_DIR = os.path.abspath(os.path.join(PROJECT_DIR, '..', 'SenseHome-6G', 'net_offload'))
if os.path.isdir(NET_OFFLOAD_DIR) and NET_OFFLOAD_DIR not in sys.path:
    sys.path.insert(0, NET_OFFLOAD_DIR)
try:
    from offload_gateway import (
        BackendResult,
        HttpJsonBackend,
        LinkSnapshot,
        OffloadGateway,
        RuleBackend,
        SubprocessPolicyClient,
        append_jsonl,
    )
except ImportError:  # pragma: no cover
    BackendResult = HttpJsonBackend = LinkSnapshot = OffloadGateway = RuleBackend = SubprocessPolicyClient = append_jsonl = None

LOCATIONS = ["客厅", "卧室", "厨房", "浴室", "书房", "室外"]
ROOM_ICONS = {"客厅": "🛋️", "卧室": "🛏️", "厨房": "🍳", "浴室": "🛁", "书房": "📚", "室外": "🌤️"}

# ============ 全局状态 ============
patrol_log = []          # 主动巡检日志
patrol_running = False   # 巡检线程状态
last_patrol_time = 0
gateway = None
gateway_backends = {}
gateway_init_error = None
backend_monitor = None
device_action_lock = threading.RLock()

# ============ 设备状态管理 ============
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except: pass
    return default_state()

def default_state():
    state = {
        "lights": {}, "temperature": {}, "curtains": {}, "sensors": {}, "security": {}
    }
    for loc in LOCATIONS:
        state["lights"][loc] = {"is_on": False, "brightness": 0}
        state["temperature"][loc] = {"temp": 25.0, "humidity": 60.0}
        state["curtains"][loc] = {"is_open": loc != "浴室", "position": 100 if loc != "浴室" else 0}
        state["sensors"][loc] = generate_sensor_data(loc)
    state["security"] = {"mode": "off", "armed": False, "alarm_triggered": False}
    return state

def save_state(state):
    # Replace atomically so the monitor/patrol/UI never reads partial JSON.
    # Errors propagate to the action boundary rather than reporting a device
    # change that was never persisted.
    directory = os.path.dirname(os.path.abspath(STATE_FILE))
    fd, temporary = tempfile.mkstemp(prefix='.sensehome-state-', suffix='.json', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(temporary, STATE_FILE)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def parse_json_request(raw_body):
    """Parse a bounded JSON object before it reaches a command or device path."""
    if not isinstance(raw_body, (bytes, bytearray)):
        raise ValueError("INVALID_REQUEST_BODY")
    if len(raw_body) > MAX_REQUEST_BODY_BYTES:
        raise ValueError("REQUEST_TOO_LARGE")
    try:
        payload = json.loads(bytes(raw_body).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("INVALID_REQUEST_JSON") from None
    if not isinstance(payload, dict):
        raise ValueError("INVALID_REQUEST_OBJECT")
    return payload


def parse_command_payload(payload):
    """Validate command input and preserve an optional structured context."""
    if not isinstance(payload, dict):
        raise ValueError("INVALID_REQUEST_OBJECT")
    command = payload.get("command")
    if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND_CHARS:
        raise ValueError("INVALID_COMMAND")
    context = payload.get("context", {})
    if not isinstance(context, dict):
        raise ValueError("INVALID_CONTEXT")
    return {"command": command, "context": context}

# ============ 传感器模拟（物理感知层） ============
SENSOR_PROFILES = {
    "客厅": {"illuminance": 15000, "pm25": 25, "co2": 500, "noise": 35, "motion_pct": 0.7},
    "卧室": {"illuminance": 8000,  "pm25": 20, "co2": 450, "noise": 25, "motion_pct": 0.5},
    "厨房": {"illuminance": 20000, "pm25": 45, "co2": 600, "noise": 55, "motion_pct": 0.3},
    "浴室": {"illuminance": 5000,  "pm25": 15, "co2": 480, "noise": 50, "motion_pct": 0.2},
    "书房": {"illuminance": 12000, "pm25": 18, "co2": 420, "noise": 20, "motion_pct": 0.4},
    "室外": {"illuminance": 60000, "pm25": 55, "co2": 420, "noise": 60, "motion_pct": 0.0},
}

def generate_sensor_data(loc):
    p = SENSOR_PROFILES.get(loc, SENSOR_PROFILES["客厅"])
    noise = lambda v: v * (0.8 + random.random() * 0.4)
    return {
        "illuminance":    round(noise(p["illuminance"]), 1),
        "motion_detected": random.random() < p["motion_pct"],
        "door_open":       loc != "室外" and random.random() < 0.05,
        "window_open":     (loc == "浴室" and random.random() < 0.3) or (loc != "室外" and random.random() < 0.08),
        "pm25":            round(noise(p["pm25"]), 1),
        "co2":             round(noise(p["co2"]), 0),
        "noise":           round(noise(p["noise"]), 1),
        "update_time":     time.strftime("%H:%M:%S")
    }

# ============ 主动环境巡检（物理AI核心） ============
def run_environment_patrol():
    """定时主动巡检：Agent自主感知环境 → 决策 → 执行"""
    global last_patrol_time
    last_patrol_time = time.time()

    state = load_state()
    actions = []
    anomalies = []

    # 1. 刷新所有传感器数据
    for loc in LOCATIONS:
        state["sensors"][loc] = generate_sensor_data(loc)

    # 2. 逐区域分析+自动决策
    for loc in LOCATIONS:
        if loc == "室外": continue
        s = state["sensors"][loc]

        # 光照自动调节窗帘（感知→执行）
        if s["illuminance"] > 50000:
            state["curtains"][loc] = {"is_open": False, "position": 20}
            actions.append(f"☀️ {loc}光照{s['illuminance']:.0f}lux→窗帘自动遮阳至20%")
        elif s["illuminance"] < 2000 and s["motion_detected"]:
            state["lights"][loc] = {"is_on": True, "brightness": 60}
            actions.append(f"💡 {loc}光照不足+有人→自动开灯60%")

        # 人体红外节能（人走灯灭）
        if not s["motion_detected"] and state["lights"][loc]["is_on"]:
            state["lights"][loc] = {"is_on": False, "brightness": 0}
            actions.append(f"🚶 {loc}无人→自动关灯（节能）")

        # PM2.5异常
        if s["pm25"] > 75:
            anomalies.append(f"⚠️ {loc} PM2.5={s['pm25']:.0f}μg/m³ 超标")
        if s["co2"] > 1000:
            anomalies.append(f"⚠️ {loc} CO2={s['co2']:.0f}ppm 偏高")

    # 3. 生成报告
    report = {
        "time": time.strftime("%H:%M:%S"),
        "actions": actions,
        "anomalies": anomalies,
        "auto_decisions": len(actions),
        "active": True  # 标记为主动巡检
    }
    patrol_log.insert(0, report)
    if len(patrol_log) > 50:
        patrol_log.pop()

    save_state(state)

    if actions or anomalies:
        print(f"[Patrol {report['time']}] 主动决策 {len(actions)} 项 | 异常 {len(anomalies)} 项")
    return report

def patrol_loop(interval):
    """后台巡检线程"""
    global patrol_running
    patrol_running = True
    print(f"[Patrol] 主动巡检线程已启动（间隔 {interval}s）")
    # 立即执行一次
    run_environment_patrol()
    while patrol_running:
        time.sleep(interval)
        if patrol_running:
            run_environment_patrol()

# ============ 多设备协同执行器 ============
def execute_leave_home(state):
    """离家模式：多设备协同编排"""
    steps = []
    anomalies = 0

    # Step 1: 刷新传感器（感知）
    for loc in LOCATIONS:
        state["sensors"][loc] = generate_sensor_data(loc)
    steps.append({
        "type": "observe",
        "step": "环境感知",
        "content": "全屋传感器数据已刷新，获得实时环境上下文"
    })

    # Step 2: 全屋关灯
    for loc in LOCATIONS:
        if loc == "室外": continue
        state["lights"][loc] = {"is_on": False, "brightness": 0}
    steps.append({
        "type": "action",
        "step": "全屋关灯",
        "content": f"已关闭 {len(LOCATIONS)-1} 个区域灯光"
    })

    # Step 3: 全屋关窗帘
    for loc in LOCATIONS:
        if loc == "室外": continue
        state["curtains"][loc] = {"is_open": False, "position": 0}
    steps.append({
        "type": "action",
        "step": "全屋关窗帘",
        "content": f"已关闭 {len(LOCATIONS)-1} 个区域窗帘"
    })

    # Step 4: 门窗检查
    for loc in LOCATIONS:
        if loc == "室外": continue
        s = state["sensors"][loc]
        if s["door_open"]:
            anomalies += 1
        if s["window_open"]:
            anomalies += 1

    steps.append({
        "type": "observe",
        "step": "安全检查",
        "content": f"门窗巡检完成，发现 {anomalies} 处异常"
    })

    # Step 5: 启动离家安防
    state["security"] = {"mode": "away", "armed": True, "alarm_triggered": False}
    steps.append({
        "type": "action",
        "step": "安防启动",
        "content": "离家安防模式已布防 🔒"
    })

    save_state(state)
    return steps, anomalies

# ============ 模糊指令处理器 ============
def handle_fuzzy_command(state, command):
    """处理模糊指令：先感知环境 → 再决策执行"""
    loc = extract_location(command) or "客厅"
    s = state["sensors"].get(loc, generate_sensor_data(loc))
    t = state["temperature"].get(loc, {"temp": 25.0, "humidity": 60.0})

    # "有点冷" / "好冷"
    if any(w in command for w in ["冷", "凉", "冻"]):
        temp = t["temp"]
        if temp < 20:
            return {
                "thought": f"用户感觉冷。查询{loc}环境：温度{temp}°C，确实偏低。物理AI自主决策：建议开启暖气。",
                "actions": [{"description": f"感知环境→{loc}温度{temp}°C<20°C→建议开启暖气至22°C"}],
                "observation": f"[Sensor] {loc}温度={temp}°C，体感偏冷",
                "answer": f"🥶 {loc}当前{temp}°C，确实偏冷。已建议开启暖气升温至22°C。这是物理AI根据实际温度做出的自主判断，而非简单响应指令。"
            }
        else:
            return {
                "thought": f"用户感觉冷。查询{loc}温度{temp}°C，实际温度正常。可能是用户个人体感差异。",
                "actions": [{"description": f"感知环境→{loc}温度{temp}°C→判断无需调节"}],
                "observation": f"[Sensor] {loc}温度={temp}°C",
                "answer": f"🤔 {loc}当前{temp}°C，实际温度正常。不过如果您觉得冷，我可以帮您调高温度。这就是物理AI的\"感知优先于执行\"原则。"
            }

    # "好热" / "太热了"
    if any(w in command for w in ["热", "烫", "闷热"]):
        temp = t["temp"]
        if temp > 27:
            return {
                "thought": f"用户感觉热。查询{loc}温度{temp}°C，确实偏高。物理AI决策：建议开空调降温+检查窗帘是否需要遮阳。",
                "actions": [
                    {"description": f"感知环境→{loc}温度{temp}°C>27°C→建议空调降温至26°C"},
                    {"description": f"检查窗帘→光照{s['illuminance']:.0f}lux→建议关闭窗帘遮阳"}
                ],
                "observation": f"[Sensor] {loc}温度={temp}°C, 光照={s['illuminance']:.0f}lux",
                "answer": f"🥵 {loc}当前{temp}°C，确实偏热！物理AI已做出综合决策：\n• 建议空调降温至26°C\n• 光照{s['illuminance']:.0f}lux较强，已建议拉窗帘遮阳\n这是多设备协同决策的结果。"
            }
        else:
            return {
                "thought": f"用户感觉热，但{loc}实际温度{temp}°C尚可。检查CO2是否导致闷热感。",
                "actions": [{"description": f"感知→温度{temp}°C正常, CO2={s['co2']:.0f}ppm"}],
                "observation": f"[Sensor] {loc}温度={temp}°C, CO2={s['co2']:.0f}ppm",
                "answer": f"💨 {loc}温度{temp}°C正常，但CO2浓度{s['co2']:.0f}ppm略高，可能是闷热的原因。建议开窗通风5分钟。"
            }

    # "太暗了"
    if any(w in command for w in ["暗", "黑", "看不清"]):
        ill = s["illuminance"]
        if ill < 3000:
            state["lights"][loc] = {"is_on": True, "brightness": 80}
            save_state(state)
            return {
                "thought": f"用户觉得暗。查询{loc}光照仅{ill:.0f}lux，确实不足。物理AI决策：自动开灯80%。",
                "actions": [{"description": f"感知→光照{ill:.0f}lux<3000lux→自动开灯80%"}],
                "observation": f"[Sensor] {loc}光照={ill:.0f}lux, [Light] 已自动开启80%",
                "answer": f"💡 {loc}光照仅{ill:.0f}lux，确实偏暗。已自动开灯至80%亮度。这是物理AI的\"感知→决策→执行\"完整闭环。"
            }
        elif ill < 10000:
            state["curtains"][loc] = {"is_open": True, "position": 100}
            save_state(state)
            return {
                "thought": f"用户觉得暗。{loc}光照{ill:.0f}lux偏低。物理AI决策：拉开窗帘利用自然光。",
                "actions": [{"description": f"感知→光照{ill:.0f}lux<10000lux→拉开窗帘采光"}],
                "observation": f"[Sensor] {loc}光照={ill:.0f}lux, [Curtain] 窗帘已全开",
                "answer": f"🪟 {loc}光照{ill:.0f}lux偏暗，已拉开窗帘利用自然光。物理AI优先使用自然光源，节能环保。"
            }
        else:
            return {
                "thought": f"{loc}光照{ill:.0f}lux充足，但用户仍觉暗。可能是窗帘或灯具问题。",
                "answer": f"🤔 {loc}光照{ill:.0f}lux已经足够，但如果您觉得暗，我可以调亮灯光。"
            }

    # "闷" / "不透气"
    if any(w in command for w in ["闷", "不透气", "憋"]):
        if s["co2"] > 800:
            state["sensors"][loc]["window_open"] = True
            save_state(state)
            return {
                "thought": f"用户感觉闷。CO2={s['co2']:.0f}ppm偏高。物理AI决策：开窗通风。",
                "actions": [{"description": f"感知→CO2={s['co2']:.0f}ppm>800ppm→自动开窗通风"}],
                "observation": f"[Sensor] {loc} CO2={s['co2']:.0f}ppm",
                "answer": f"🌬️ CO2浓度{s['co2']:.0f}ppm偏高，已开窗通风。物理AI的\"感知→执行\"：发现CO2超标→自动开窗。"
            }
        return {
            "thought": f"用户感觉闷。CO2={s['co2']:.0f}ppm正常，温度{t['temp']}°C。建议检查湿度。",
            "answer": f"💧 {loc}空气指标正常（CO2={s['co2']:.0f}ppm），但湿度{t['humidity']:.0f}%。如果是湿闷，建议开启除湿。"
        }

    return None  # 非模糊指令，走正常流程

# ============ 正常指令执行 ============
def execute_command(state, command):
    """标准 ReAct 推理执行（灯光/温湿度/场景触发）"""
    cmd = command.strip()
    result = {"thought": "", "actions": [], "observation": "", "answer": ""}

    # 开灯
    m = re.search(r'(打开|开)(.+?)的?灯', cmd)
    if m:
        loc = extract_location(cmd) or "客厅"
        state["lights"][loc] = {"is_on": True, "brightness": 100}
        result["thought"] = f'用户想打开{loc}灯光 → 调用 light_control'
        result["actions"].append({"description": f'light_control(location="{loc}", action="开灯")'})
        result["observation"] = f"[LightControl] {loc}灯光已打开，亮度100%"
        result["answer"] = f"✅ {loc}的灯已打开，亮度100%"
        save_state(state)
        return result

    # 全屋关灯
    if re.search(r'关.*(所有|全屋|全部).*灯', cmd) or cmd in ["关灯", "关闭所有灯"]:
        for loc in LOCATIONS:
            if loc != "室外":
                state["lights"][loc] = {"is_on": False, "brightness": 0}
        result["thought"] = '用户想关闭全屋灯光 → 多设备协同执行'
        result["actions"].append({"description": "light_control(location=\"全屋\", action=\"关灯\")"})
        result["observation"] = f"[LightControl] 全屋灯光已关闭（{len(LOCATIONS)-1}个区域）"
        result["answer"] = "✅ 全屋灯光已关闭"
        save_state(state)
        return result

    # 单区关灯
    m = re.search(r'关(闭|掉)?(.+?)的?灯', cmd)
    if m:
        loc = extract_location(cmd) or "客厅"
        state["lights"][loc] = {"is_on": False, "brightness": 0}
        result["thought"] = f'用户想关闭{loc}灯光'
        result["actions"].append({"description": f'light_control(location="{loc}", action="关灯")'})
        result["observation"] = f"[LightControl] {loc}灯光已关闭"
        result["answer"] = f"✅ {loc}的灯已关闭"
        save_state(state)
        return result

    # 调亮度（上下文记忆）
    m = re.search(r'(调暗|调亮|调节|亮度|调到)', cmd)
    if m:
        loc = extract_location(cmd) or "客厅"
        prev = state["lights"].get(loc, {"brightness": 80})
        prev_b = prev.get("brightness", 80) if prev.get("is_on", False) else 80
        bm = re.search(r'(\d+)\s*%?', cmd)
        if bm:
            new_b = min(max(int(bm.group(1)), 0), 100)
        elif '调暗' in cmd:
            new_b = max(prev_b - 20, 10)
            result["thought"] = f'上下文记忆：{loc}上次亮度{prev_b}% → 调暗20%至{new_b}%'
        elif '调亮' in cmd:
            new_b = min(prev_b + 20, 100)
            result["thought"] = f'上下文记忆：{loc}上次亮度{prev_b}% → 调亮20%至{new_b}%'
        else:
            new_b = 50

        state["lights"][loc] = {"is_on": new_b > 0, "brightness": new_b}
        result["actions"].append({"description": f'light_control(location="{loc}", brightness={new_b})'})
        result["observation"] = f"[LightControl] {loc}亮度 {prev_b}%→{new_b}%"
        result["answer"] = f"✅ {loc}灯光已从{prev_b}%调至{new_b}%（基于上下文记忆）"
        save_state(state)
        return result

    # 温湿度
    if re.search(r'(温度|室温|湿度|多少度)', cmd):
        loc = extract_location(cmd) or "客厅"
        temp = round(24.5 + random.uniform(-1.5, 2.0), 1)
        hum = round(min(max(55 + random.uniform(-10, 15), 30), 90), 0)
        state["temperature"][loc] = {"temp": temp, "humidity": hum}
        feel = "舒适" if 20 <= temp <= 26 else ("偏冷" if temp < 20 else "偏热")
        result["thought"] = f'查询{loc}温湿度 → read_temperature'
        result["actions"].append({"description": f'read_temperature(location="{loc}")'})
        result["observation"] = f"[TempSensor] {loc}温度={temp}°C，湿度={hum}%"
        result["answer"] = f"🌡️ {loc}当前{temp}°C（体感{feel}），湿度{hum}%"
        save_state(state)
        return result

    # 早安场景
    if '早上好' in cmd:
        state["lights"]["客厅"] = {"is_on": True, "brightness": 40}
        state["temperature"]["客厅"] = {"temp": 25.0, "humidity": 60.0}
        result["thought"] = '早安场景Skill：读温湿度→调灯光→播报'
        result["actions"].append({"description": 'read_temperature(location="客厅")'})
        result["actions"].append({"description": 'light_control(location="客厅", brightness=40)'})
        result["observation"] = "[TempSensor] 25.0°C/60% | [Light] 柔光40%"
        result["answer"] = "☀️ 早安！室内25°C/60%，灯光已调至柔光模式。Have a nice day!"
        save_state(state)
        return result

    # 窗帘控制
    m = re.search(r'(拉开|打开|关闭|拉上)(.+?)窗帘', cmd)
    if m:
        loc = extract_location(cmd) or "客厅"
        is_open = "开" in m.group(1)
        state["curtains"][loc] = {"is_open": is_open, "position": 100 if is_open else 0}
        result["thought"] = f'用户想{("拉开" if is_open else "关闭")}{loc}窗帘'
        result["actions"].append({"description": f'curtain_control(location="{loc}", {"开" if is_open else "关"})'})
        result["answer"] = f"✅ {loc}窗帘已{'拉开' if is_open else '关闭'}"
        save_state(state)
        return result

    return None  # 交给模糊指令处理器

# ============ 辅助 ============
def extract_location(text):
    for loc in LOCATIONS:
        if loc in text:
            return loc
    aliases = {"起居室": "客厅", "主卧": "卧室", "卫生间": "浴室", "厕所": "浴室",
               "WC": "浴室", "办公室": "书房", "户外": "室外"}
    for a, r in aliases.items():
        if a in text: return r
    return None


# ============ AI+6G 卸载网关 ============
class _UnavailableBackend:
    def __init__(self, name):
        self.name = name

    def infer(self, command, context=None):
        return BackendResult(False, False, {}, [], error="BACKEND_NOT_CONFIGURED", source=self.name)

    def health(self):
        return {"ready": False, "backend": self.name, "error": "BACKEND_NOT_CONFIGURED"}


class _UnavailablePolicy:
    def update_links(self, link):
        return 0.0

    def decide(self, task_class, link):
        return "local-rules", "POLICY_UNAVAILABLE", "C policy_cli unavailable"


def _gateway_local_handler(command, context=None):
    """将既有物理规则执行器注入 local-rules 后端。"""
    state = load_state()
    if OffloadGateway and OffloadGateway.classify(command) == 'safety':
        if command.strip() == '查询安防状态':
            security = state.get('security', {})
            return {'answer': '演示安防状态：' + ('已布防' if security.get('armed') else '未布防'),
                    'actions': [], 'observation': json.dumps(security, ensure_ascii=False),
                    'semantic_complete': True}
        return {'answer': '安全相关控制未获授权或未接入设备，请在现场确认并操作。',
                'actions': [], 'semantic_complete': False}
    if OffloadGateway and OffloadGateway.needs_plan(command):
        return {'answer': '该指令包含多个目标、条件或否定要求，本地规则不执行部分动作；请拆成明确的单条指令。',
                'actions': [], 'semantic_complete': False}
    if any(w in command for w in ["我出门了", "拜拜", "再见", "离家模式"]):
        steps, anomalies = execute_leave_home(state)
        return {
            "thought": "物理AI多设备协同：离家模式启动。",
            "actions": [s for s in steps if s["type"] == "action"],
            "observation": "\n".join(s["content"] for s in steps if s["type"] == "observe"),
            "answer": f"离家模式已启动，发现门窗异常{anomalies}处。",
            "semantic_complete": True,
        }
    fuzzy = handle_fuzzy_command(state, command)
    if fuzzy:
        fuzzy["semantic_complete"] = True
        return fuzzy
    standard = execute_command(state, command)
    if standard:
        standard["semantic_complete"] = True
        return standard
    return {
        "thought": f'无法解析"{html_escape(command)}"，已安全停留在本地规则层。',
        "actions": [],
        "answer": f'暂不理解"{html_escape(command)}"。',
        "semantic_complete": False,
    }


def _gateway_action_executor(actions, context=None):
    """执行白名单结构化意图；不解释模型生成的命令文本。"""
    with device_action_lock:
        state = load_state()
        result = execute_device_actions(state, actions)
        save_state(state)
        return result


def apply_light_request(payload):
    """Apply the explicit light API through the same allowlisted executor."""
    if not isinstance(payload, dict):
        raise ValueError("INVALID_REQUEST_OBJECT")
    location = payload.get("location")
    action = payload.get("action", "on")
    if not isinstance(location, str) or location not in LOCATIONS:
        raise ValueError("INVALID_LOCATION")
    if action not in {"on", "off", "set"}:
        raise ValueError("INVALID_LIGHT_ACTION")
    params = {"location": location, "action": action}
    if action == "set":
        brightness = payload.get("brightness")
        if type(brightness) is not int or not 0 <= brightness <= 100:
            raise ValueError("INVALID_BRIGHTNESS")
        params["brightness"] = brightness
    elif "brightness" in payload:
        raise ValueError("INVALID_LIGHT_PARAMETERS")
    return _gateway_action_executor([{"name": "light_control", "params": params}])


def _probe_gateway_links():
    """返回后台探测缓存；PC 模拟器内设备可达性与模型就绪分开记录。"""
    if backend_monitor is None:
        return LinkSnapshot(lan_ok=True, lan_rtt_ms=0.0)
    return LinkSnapshot(**backend_monitor.snapshot()["link"])


def _init_gateway():
    global gateway, gateway_backends, gateway_init_error, backend_monitor
    gateway_backends = {"local-rules": RuleBackend(handler=_gateway_local_handler)} if RuleBackend else {}
    cloud_url = os.environ.get("SENSEHOME_CLOUD_URL", "").strip()
    local_url = os.environ.get("SENSEHOME_LOCAL_URL", "").strip()
    def _backend_timeout(name, default):
        try:
            return max(0.05, float(os.environ.get(name, str(default))))
        except (TypeError, ValueError):
            return default
    # Health polling must remain short so a dead model does not block the
    # monitor. Inference gets its own budget because real GPU generation may
    # legitimately take longer than /health or /probe.
    infer_timeout_s = _backend_timeout("SENSEHOME_INFER_TIMEOUT_S", 2.0)
    health_timeout_s = _backend_timeout("SENSEHOME_HEALTH_TIMEOUT_S", 0.5)
    if HttpJsonBackend:
        gateway_backends["cloud-model"] = (
            HttpJsonBackend(cloud_url, "cloud-model", timeout_s=infer_timeout_s,
                           health_timeout_s=health_timeout_s, inference_timeout_s=infer_timeout_s)
            if cloud_url else _UnavailableBackend("cloud-model")
        )
        gateway_backends["local-model"] = (
            HttpJsonBackend(local_url, "local-model", timeout_s=infer_timeout_s,
                           health_timeout_s=health_timeout_s, inference_timeout_s=infer_timeout_s)
            if local_url else _UnavailableBackend("local-model")
        )
    if backend_monitor:
        backend_monitor.close()
    backend_monitor = BackendMonitor(gateway_backends).start()
    atexit.register(backend_monitor.close)

    policy = _UnavailablePolicy()
    cli_name = "policy_cli.exe" if os.name == "nt" else "policy_cli"
    cli_path = os.environ.get("SENSEHOME_POLICY_CLI", os.path.join(NET_OFFLOAD_DIR, cli_name))
    if SubprocessPolicyClient and os.path.isfile(cli_path):
        try:
            policy = SubprocessPolicyClient(cli_path)
            atexit.register(policy.close)
        except Exception as exc:  # noqa: BLE001
            gateway_init_error = f"policy_cli_init:{type(exc).__name__}"
    if OffloadGateway and RuleBackend:
        event_path = os.environ.get("SENSEHOME_EVENT_LOG", os.path.join(PROJECT_DIR, "offload_events.jsonl"))
        gateway = OffloadGateway(
            policy=policy,
            backends=gateway_backends,
            link_snapshot=LinkSnapshot(),
            link_probe=_probe_gateway_links,
            event_sink=append_jsonl(event_path) if append_jsonl else None,
            action_executor=_gateway_action_executor,
        )


def gateway_health():
    if gateway is None:
        return {"ready": False, "error": gateway_init_error or "GATEWAY_UNAVAILABLE"}
    # Serving health must never wait for two sequential model HTTP timeouts.
    health = backend_monitor.snapshot() if backend_monitor else gateway.health()
    if gateway_init_error:
        health["init_error"] = gateway_init_error
    return health


def _legacy_process_command(command):
    state = load_state()
    if any(w in command for w in ["我出门了", "拜拜", "再见", "离家模式"]):
        steps, anomalies = execute_leave_home(state)
        return {
            "thought": "物理AI多设备协同：离家模式启动。感知环境→逐一关灯→关窗帘→安全检查→启动安防。",
            "actions": [s for s in steps if s["type"] == "action"],
            "observation": "\n".join([s["content"] for s in steps if s["type"] == "observe"]),
            "answer": f"🏠 离家模式已启动！物理AI执行报告：\n✅ 全屋灯光已关闭\n✅ 全屋窗帘已拉上\n🔒 离家安防已布防\n⚠️ 门窗异常{anomalies}处",
        }
    fuzzy = handle_fuzzy_command(state, command)
    if fuzzy:
        return fuzzy
    standard = execute_command(state, command)
    if standard:
        return standard
    return {
        "thought": f'无法解析"{command}"。尝试物理AI模糊理解...',
        "actions": [{"description": "模糊指令匹配失败"}],
        "answer": f'🤔 抱歉，暂不理解"{command}"。',
    }


def process_command(command, context=None):
    if gateway is None:
        return _legacy_process_command(command)
    request_context = dict(context or {})
    if backend_monitor:
        monitored = backend_monitor.snapshot()
        request_context["observation_age_ms"] = monitored.get("age_ms")
        cloud = monitored.get("backends", {}).get("cloud-model", {})
        fields = ("model_ready", "queue_ms", "free_mem_mb", "inflight")
        observed = {field: cloud.get(field) for field in fields}
        if "model_ready" not in cloud:
            observed["model_ready"] = cloud.get("ready")
        field_sources = {field: "backend_health_cache" for field in fields}
        if cloud.get("queue_scope") == "wrapper_fail_fast_admission":
            # The adapter has no waiting queue; that does not observe the
            # upstream model server's queue. Carry missing telemetry as null.
            observed["queue_ms"] = cloud.get("upstream_queue_ms")
            field_sources["queue_ms"] = "upstream_queue_ms"
            field_sources["inflight"] = "wrapper_inflight"
        request_context["compute"] = {
            **observed, "source": "backend_health_cache", "field_sources": field_sources,
        }
    event = gateway.handle(command, request_context)
    result = dict(event.get("response") or {})
    for key in ("event_id", "task_class", "backend", "reason", "policy_log", "link", "compute", "latency", "semantic_complete", "interface_ok", "model_ms", "fallback_from", "fallback_attempted", "refusal_reason", "error", "source"):
        if key in event:
            result[key] = event[key]
    result["gateway_event"] = event
    return result

# ============ HTTP 服务器 ============
class BridgeHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        path = urlparse(self.path).path

        if path == '/' or path == '/index.html':
            self.serve_file('web_panel.html', 'text/html; charset=utf-8')
        elif path == '/api/state':
            state = load_state()
            self.send_json(state)
        elif path == '/api/health':
            self.send_json(gateway_health())
        elif path == '/api/patrol/log':
            self.send_json({"log": patrol_log, "running": patrol_running,
                           "last": time.strftime("%H:%M:%S", time.localtime(last_patrol_time)) if last_patrol_time else "从未"})
        elif path == '/api/patrol/trigger':
            report = run_environment_patrol()
            self.send_json({"status": "ok", "report": report})
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == '/api/command':
            try:
                data = parse_command_payload(parse_json_request(self._read_body()))
                self.send_json(process_command(data["command"], data["context"]))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)

        elif path == '/api/light':
            try:
                result = apply_light_request(parse_json_request(self._read_body()))
                self.send_json({"status": "ok", **result})
            except (ValueError, OSError) as exc:
                self.send_json({"error": str(exc)}, status=400)

        elif path == '/api/patrol/toggle':
            global patrol_running
            patrol_running = not patrol_running
            if patrol_running:
                t = threading.Thread(target=patrol_loop, args=(PATROL_INTERVAL,), daemon=True)
                t.start()
            self.send_json({"patrol_active": patrol_running})

        else:
            self.send_error(404)

    def _read_body(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
        except (TypeError, ValueError):
            raise ValueError("INVALID_CONTENT_LENGTH") from None
        if length < 0 or length > MAX_REQUEST_BODY_BYTES:
            raise ValueError("REQUEST_TOO_LARGE")
        return self.rfile.read(length)

    def serve_file(self, filename, content_type):
        filepath = os.path.join(SCRIPT_DIR, filename)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', len(content.encode('utf-8')))
            self.end_headers()
            self.wfile.write(content.encode('utf-8'))
        except FileNotFoundError:
            self.send_error(404)

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, indent=2)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', len(body.encode('utf-8')))
        self.end_headers()
        self.wfile.write(body.encode('utf-8'))

    def log_message(self, format, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {args[0]}")

try:
    _init_gateway()
except Exception as exc:  # pragma: no cover
    gateway_init_error = f"gateway_init:{type(exc).__name__}"
    gateway = None


def main():
    port = PORT
    patrol_sec = PATROL_INTERVAL
    for i, arg in enumerate(sys.argv):
        if arg == '--port' and i+1 < len(sys.argv): port = int(sys.argv[i+1])
        if arg == '--patrol-interval' and i+1 < len(sys.argv): patrol_sec = int(sys.argv[i+1])

    state = load_state()
    save_state(state)

    print(f"""
╔══════════════════════════════════════════════════════╗
║  openvela 物理AI — Web Dashboard 桥接服务器 v2.0     ║
╠══════════════════════════════════════════════════════╣
║  🌐 http://localhost:{port}                           ║
║                                                      ║
║  物理AI 核心特性:                                      ║
║  🧠 模糊指令理解 - "有点冷"/"太暗了"/"不透气"          ║
║  🔄 主动环境巡检 - 每{patrol_sec}s自主感知+决策         ║
║  📡 多传感器感知 - 光照/人体/门磁/PM2.5/CO2/噪音       ║
║  🤝 多设备协同   - 离家模式灯光+窗帘+安防联动           ║
║  💾 状态记忆     - 上下文感知+持久化                    ║
║                                                      ║
║  API:                                                ║
║  GET  /api/state        - 设备状态                    ║
║  POST /api/command      - 自然语言指令                 ║
║  GET  /api/patrol/log   - 主动巡检日志                 ║
║  GET  /api/patrol/trigger - 手动触发巡检               ║
╚══════════════════════════════════════════════════════╝
""")

    # 启动主动巡检线程
    if patrol_sec > 0:
        t = threading.Thread(target=patrol_loop, args=(patrol_sec,), daemon=True)
        t.start()

    server = http.server.HTTPServer(('0.0.0.0', port), BridgeHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        global patrol_running
        patrol_running = False
        print("\n服务器已停止")
        server.shutdown()

if __name__ == '__main__':
    main()
