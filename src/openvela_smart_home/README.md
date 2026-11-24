# openvela 家居设备组件

这是 SenseHome 项目中的设备侧 C 语言组件，提供灯光、温湿度、窗帘、安防和环境监测工具，以及命令白名单。组件面向 openvela/NuttX API，并带有 QEMU goldfish-arm64 自动构建脚本。

当前华为 ICT 创新赛 MVP 的提案与执行入口位于同一仓库的 `../SenseHome-6G/v11/`。本组件和 Python Runtime 尚未通过适配接口连通。QEMU 与 C 工具测试不代表真实家庭设备验证。

## 本地 C 工具测试

在支持 GCC、Make 和 POSIX shell 的环境中运行：

```sh
make
make test
make security
```

QEMU/openvela 交叉构建需要网络和相应的上游工具链。运行 `./auto_build_vela.sh` 会下载 openvela manifest 和源码。

## 目录

- `tools/` 保存设备工具和软件状态模拟。
- `security/` 保存命令白名单。
- `dashboard/` 提供本机 Web 演示桥接。
- `patches/` 保存应用到 openvela 上游的集成补丁。
- `skills/` 保存沿用的场景定义，供开发参考。使用前请核对各场景需要的设备工具和触发接口。
- `LICENSE`、`NOTICE` 与 `docs/THIRD_PARTY.md` 记录许可和上游来源。
