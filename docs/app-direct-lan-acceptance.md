# 智慧生活直连 LAN 验收准备与当前结果

本轮只使用已配对的独立实验身份、实际 ARM HA 镜像和 `oh_single_` 虚拟实体。没有连接原 HA 的 API、安装组件、切换引擎或操作家庭灯光和米家自动化。原生 x86 主机仍缺少，不能把本轮 ARM 结果当作 x86 验收。

## 当前状态

MatePad 的新调试连接已经建立，读取到的型号是 `LRT-W00`；智慧生活保持登录。只读设备信息中的旧灯 SN 与私有实验清单精确一致，未按名称直接操作家庭设备。

独立 HA 加载新 C 后，确认保留同一配置条目、同一配对资料和一个原生所有者。SDK 收到云端上线事件、进入 ONLINE 状态，14 张可用虚拟子卡的上线请求收到成功响应；预置离线卡仍隔离。但 **App 中实验卡片显示离线**。SDK 上线事件不能代替 App 验收，当前还没有发出“空场景”测试控制，不能列为首次控制通过。

App 观察夹具补充两个字段：一次 HA 启动的随机 `acceptance_run_id`，以及观测标记之后 C 写入的 `backend_result`。后者只保留请求编号、结果类别、安全异常类型、errno 和耗时。它与实际 HA 调用、Python 路由结果、C `write_confirmed` 一起核对，测试工具本身不替代 App 发出控制，不增加重试。

重启复核另发现旧观察器的初始 C 日志偏移为零，会把历史写入确认混进新一轮 `ready`／`status`。现在在启动 HA 和 SDK **之前**记录服务审计和原生日志的末尾：排除旧记录，同时不漏记启动期间的新写入。显式 `mark` 仍会重新设定本次点击的起点。该修复只影响实验观察器，不修改桥接控制行为。

已保留含 136 条历史 C 确认的真实原生日志，分别用修复后的观察器冷启动和再次启动实际 HA：两个新轮次都正确显示零次 HA 调用、零次 C 写入确认；启动标识不同，虚拟状态、配置条目及配对资料相同，均只有一个原生所有者。停止后核对零所有者。这证明了独立重启和观察记录的隔离，**没有证明 App 控制恢复**。脱敏断言见[本轮记录](plans/app-direct-lan-preparation-validation.json)。

局域网链路尚未建立：现有 Lima 为 NAT；没有安装 `socket_vmnet`，本机非交互 sudo 返回需要管理员密码。原有 SSH/UDP 转发不能作为 App 直连证据。本轮准备了独立的桥接配置候选文件，没有替换原 VM 配置，也没有改原 HA 的网络。

## 已准备的临时桥接入口

上游 `socket_vmnet` v1.2.2 arm64 官方压缩包已经下载到本机私有实验目录，并核对 GitHub Release 公布的 SHA256：

```text
archive: c7bf62308fbcfdc29bdfb8373c9b1951f7ac2396446e4390919796a94972e6dc
binary:  b8a72a62237312f2f756027dea504a844edeb40014702d4a320292c026d282b0
```

脚本 [ha_macos_lab_lan.sh](../tools/ha_macos_lab_lan.sh) 支持 `check /path/to/socket_vmnet` 和 `run /path/to/socket_vmnet`。前者不修改系统；后者需要用户在本机终端通过 sudo 启动。参数为官方二进制路径，脚本再次校验固定摘要，将程序复制到 root 管理的临时目录并复核，然后在 `en0` 建立实验桥接。

脚本不写 sudoers、不安装 LaunchDaemon、不替换现有 Lima 配置，也不执行 HA 命令。管理员启动后保持终端；按 Ctrl-C 终止桥接并删除它创建的运行文件。若临时目录已存在则拒绝覆盖，避免终止其他会话。

独立 Lima 候选网络配置为：

```yaml
networks:
  - socket: /private/var/run/hwhomebridge-lan-test/bridged.sock
    macAddress: "52:55:55:60:3b:0e"
    interface: lima0
```

这是既有**实验身份**使用的 MAC，不能复制给生产网关。候选已通过 `limactl validate`；必须先确认实验 SDK 全部停止，再备份并应用 VM 配置。运行时只启用一个使用该配对身份的容器，直接使用 VM 的局域网接口；不能同时启动保留的 NAT 或模拟 x86 SDK。

Lima 的桥接要求及管理员安装方式见 [Lima VMNet 文档](https://lima-vm.io/docs/config/network/vmnet/)，组件选项见 [socket_vmnet 上游说明](https://github.com/lima-vm/socket_vmnet/tree/v1.2.2)，二进制可从[对应 Release](https://github.com/lima-vm/socket_vmnet/releases/tag/v1.2.2) 获取。本轮采用临时运行方式，未添加官方示例中的持久 sudoers 授权。

## 后续实际验收步骤

1. 确认临时 socket 可用，应用已校验的独立 Lima 网络候选，获得同一家庭 LAN 的独立 IP。只为实验运行目录调整接口和发现端口；已有根、子 SN、配对文件及配置条目保持。
2. 用独立实验容器的 host 网络运行 SDK，确认 UDP 5683 发现、5686 认证控制和回报通道可达，避免把可配置的发现端口误当作全部本地控制端口。不使用 Mac 代理、SSH/UDP 转发或原 HA 作中转。
3. 在实验 Linux 上仅记录 MatePad 与实验 IP 之间的发现、协商、加密控制端点和时间；原始内容留在私有目录。限定实验进程的 WAN 通道来排除云端回退，不能断家庭路由器或生产服务的网络。
4. App 设备信息精确核对 `effects-2` 实验子 SN。冷启动 HA 前把虚构主灯设为关闭及其他灯效，记录零控制基线；启动后不预热控制、不补发失败请求。标记后只在 App 点击一次“空场景”开启。
5. 同时要求：确实观察到 App 直连 LAN 请求、一次 HA `light.turn_on`、目标灯效为“空场景”、一次 C 接受结果及一次 `write_confirmed`，其他虚拟实体不变。超时或重试后才成功必须保留首个失败，不能改记为首次成功。
6. 优雅停止独立 HA，核对零原生所有者和配对文件未变；用新 HA 进程恢复同一条目，核对灯效读回和一个所有者，再从 App 做重启后的第一次开／关。分别保存首次、恢复读回和首次后续控制的证据。
7. 结束时停止实验所有者，恢复 VM 网络配置和初始停机状态、恢复 MatePad 熄屏设置；用户终端 Ctrl-C 清理临时桥接。脱敏记录与实际结果一致后提交。

在本地管理员步骤完成和 App 真实直连证据取得前，`app_direct_lan_verified` 与 `empty_scene_first_control_verified` 均保持 false。没有据此解除生产接管保护，也没有重打现有 b8 安装包。

完整历史改动范围见[改动清单](change-inventory.md)。Jev 只参与源文件职责分类，身份核对、网络路径判断和通过条件由实际记录与确定性检查决定。
