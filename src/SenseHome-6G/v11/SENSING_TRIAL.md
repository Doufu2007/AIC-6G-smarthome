# CSI、世界模型与 RSI 离线试验

在项目的 `src/SenseHome-6G/v11` 目录运行以下命令。仅使用 Python 标准库。

```powershell
python -X utf8 -B sensing.py
python -X utf8 -B -m unittest discover -s tests -p test_sensing.py -v
```

默认输出明确标记为 `offline_fixture`，CSI 帧、窗帘状态和标签均为手工构造。输出内的案例计数、预测频率和候选通过情况用于核对代码行为，实采准确率和时延仍需另外测量。

## 回放 ESP-CSI 记录

按 Espressif 的 `get-started` 示例，用与开发板实际芯片一致的 ESP-IDF target 分别刷写 `csi_send` 和 `csi_recv`。记录接收端串口输出，每个文件保留同一发送器、信道、包格式和短时间窗口的数据。接收端重启后另启文件。 [ESP-CSI 采集步骤与格式](https://github.com/espressif/esp-csi/tree/master/examples/get-started)

```powershell
python -X utf8 -B sensing.py --csv 'C:\path\to\window.csv'
```

程序识别含成对 I/Q 分量的 `CSI_DATA` 行，忽略串口日志。它会丢弃重复帧、错误长度和不匹配的帧，按相邻帧子载波幅度变化输出 `motion` 或 `quiet`。有效帧太少或无效帧占多数时输出 `missing`，得分为空。`valid_ratio` 统计收到的 CSI 文本行中可用行的比例，链路丢包率需要独立测量。默认阈值只是原型参数，部署前应以本房间的静止、走动与遮挡样本标定。两类事件都不用于触发设备动作或判断跌倒。

## 模型与版本试验

世界模型原型根据当前窗帘状态、候选动作和 CSI 活动标签，从有标注的历史转移计数预测下一状态。缺少 CSI 时使用状态与动作统计，遇到未见过的状态动作或并列时返回 `unknown`。`confidence` 是样本内频率，`support` 是对应样本数量。

RSI 工程原型评估给定候选。当同一留出集上正确案例严格增加且旧版本已答对的案例均保持正确时，才允许显式启用。版本切换后会清空此前基于旧版本的待启用评估。演示随后回滚。版本和操作记录保留在本次进程的报告内。自动漂移检测、补采和参数更新设置适合在真样本具备后另行研究。

实采验证可在一间房记录 `初始状态、开/关/停止动作、CSI 窗口、独立行程传感器标签`，以采集轮次划分训练和留出数据，并对比同一留出集上的无 CSI 模型和加 CSI 模型。活动标签不能当作行程真值。录像与 CSI 原始记录留在本地试验环境。
