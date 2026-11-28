# SenseHome 全屋设备能力与权限边界手册（AgentArts RAG 语料）

- **文档版本**：`2026.11`
- **说明**：实时在线状态与开关值必须以 `sensehome_list_devices` / `sensehome_get_device_state`（或对应 REST 只读接口）返回为准，本手册提供稳定的房间映射、别名对照与权限分级说明。

## 1. 可控执行设备（`access = control`，仅允许生成待确认提案）

| Entity ID | 房间 | 名称 | 常用别名 | 允许服务 | 参数约束 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `light.reading` | 书房 | 阅读灯 | 桌旁灯 | `light.turn_on`, `light.turn_off` | `brightness_pct` 为整数 `0..100` |
| `cover.study` | 书房 | 书房窗帘 | 阅读区窗帘 | `cover.open_cover`, `cover.close_cover` | `data` 必须为空 `{}` |
| `light.living` | 客厅 | 客厅顶灯 | 起居灯 | `light.turn_on`, `light.turn_off` | `brightness_pct` 为整数 `0..100` |
| `climate.living` | 客厅 | 客厅空调 | - | `climate.set_temperature` | 仅制冷/制热模式生效，范围 `16..30°C`，步长 `0.5°C` |
| `light.patio` | 阳台 | 阳台灯 | - | `light.turn_on`, `light.turn_off` | `brightness_pct` 为整数 `0..100` |
| `switch.patio` | 阳台 | 阳台插座 | - | `switch.turn_on`, `switch.turn_off` | `data` 必须为空 `{}` |
| `light.bedroom` | 卧室 | 卧室灯 | - | `light.turn_on`, `light.turn_off` | `brightness_pct` 为整数 `0..100` |
| `light.kitchen` | 厨房 | 厨房灯 | - | `light.turn_on`, `light.turn_off` | `brightness_pct` 为整数 `0..100` |
| `light.bathroom` | 卫生间 | 卫生间灯 | - | `light.turn_on`, `light.turn_off` | `brightness_pct` 为整数 `0..100` |
| `light.entry` | 门口 | 门口灯 | - | `light.turn_on`, `light.turn_off` | `brightness_pct` 为整数 `0..100` |

## 2. 只读与仅事件设备（`access = read_only / event_only`，严禁控制）

| Entity ID | 房间 | 名称 | 别名 | 权限级别 | 状态含义 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `binary_sensor.front_door` | 门口 | 入户门磁 | 大门门磁 | `read_only` | `on` = 打开（未关闭），`off` = 已关闭，`unknown/unavailable` = 无法核实 |
| `sensor.living_temperature` | 客厅 | 客厅温度 | - | `read_only` | 数值 + 单位 `°C` |
| `camera.front_door` | 门口 | 门口监控 | 门口摄像头 | `event_only` | 仅返回 `online` / `unavailable`，绝不传输画面 |
