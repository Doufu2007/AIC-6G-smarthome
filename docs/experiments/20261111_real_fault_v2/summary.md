# 真实模型与受控HTTP代理验证

各端点仍为同机模型；HTTP503及700ms准入等待发生在自建代理中，均为软件实验条件，不是GPU队列、真实WAN故障或6G测量。设备为隔离JSON模拟。

|条件|策略|非空模拟完成|代理POST尝试|实际转发模型POST|全请求中位ms|
|---|---|---:|---:|---:|---:|
|healthy|fixed-local|6/6|6|6|729.913|
|healthy|fixed-cloud|6/6|6|6|1113.954|
|healthy|network-only|6/6|6|6|721.004|
|healthy|joint|6/6|6|6|708.981|
|cloud_http503|fixed-local|6/6|6|6|712.716|
|cloud_http503|fixed-cloud|0/6|6|0|5.516|
|cloud_http503|network-only|6/6|6|6|714.764|
|cloud_http503|joint|6/6|6|6|696.539|
|local_http503|fixed-local|0/6|6|0|4.985|
|local_http503|fixed-cloud|6/6|6|6|1054.510|
|local_http503|network-only|0/6|6|0|5.185|
|local_http503|joint|6/6|6|6|1108.434|
|cloud_admission700|fixed-local|6/6|6|6|714.990|
|cloud_admission700|fixed-cloud|6/6|6|6|1790.389|
|cloud_admission700|network-only|6/6|6|6|719.141|
|cloud_admission700|joint|6/6|6|6|712.163|
|local_admission700|fixed-local|6/6|6|6|1420.246|
|local_admission700|fixed-cloud|6/6|6|6|1046.806|
|local_admission700|network-only|6/6|6|6|1431.163|
|local_admission700|joint|6/6|6|6|1042.006|

每条件独立重建C策略状态，条件内重复连续。计分直接复用冻结运行器，失败/回退/保护拒绝与完成分开；主矩阵与每条件两次预热分开。2条已有人工命令的重复是受控回归，不是新用户泛化。不同条件不混算“真实世界可靠性”。

完整来源、注册、代理HTTP原文及逐条评分见registration.json、各conditions目录；旧600行及生产源码未修改。
