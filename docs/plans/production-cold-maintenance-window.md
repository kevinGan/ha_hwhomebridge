# 生产冷备维护窗口：执行、撤销与恢复

2026-10-17。当前只完成只读准备和独立演练；**没有获批或执行原机停机、冷备、传包、部署、后端切换**。下面是未来窗口的人工运行手册，不是可直接执行的自动化。窗口只保存原网关快照，结束后仍运行原代码、原配置条目和原网关，不启用 OpenHarmony，不创建第二个网关。

## 本次只读复核

原机 HA OS 18.3 / Core 2026.10.1 / Supervisor 2026.09.3，amd64；宿主 Docker 29.7.2，Core 使用 host 网络、运行 UID 0，重启策略为 `no`。独立宿主 SSH 已可用；宿主没有 Python，故使用已独立验证的[免安装运行时](../portable-cold-runtime.md)。这些是准备时观察，窗口前必须重查，不能永久沿用。

新清单仍识别 32 个旧设备，书房 13 个显式角色和 7 张组合卡的只读状态投影通过。接管阻塞仍有两项：书房旧灯服务选取随实体顺序改变；厨房灯组一个实体不可用。它们不妨碍保存原样冷备，但**阻止把冷备当成可接管配置**。

书房旧 Huawei 设备只有一个 PID `002`，服务为 `switch`、`brightness`、`cct`。观察顺序重放三项均选主灯，氛围灯也是候选。当前部署版本没有运行时映射查询接口，新读取的已有 Core 日志也没有目标路由。因此实际旧绑定继续标记“未确认”；不宣称氛围灯已有独立旧卡，不在原机冻结或覆盖绑定。

私有复核包包括：两次静态组件及所属配置条目观察、两次宿主 Docker 证据、双遍实体清单、已有日志审计、显式角色文件及映射配置。它们保留本机私有目录；Git 中只保存计数、布尔结果和脱敏演练。`tools/ha_maintenance_review.py` 在本机离线比较这些观察，文件、条目、Core 启动记录、宿主身份或实体预检配置不一致时返回失败；无网络或控制入口，也不签发部署许可。

```sh
# 完全离线，仅读取已有私有观察。失败时不可用这份清单放行后续流程。
python3 tools/ha_maintenance_review.py \
  --before /secure/online-before --after /secure/online-after \
  --host-before /secure/host-before --host-after /secure/host-after \
  --preflight /secure/entities/preflight.private.json \
  --binding /secure/binding/binding-audit.private.json
```

## 窗口前需要落实

1. 单独取得**原机冷备窗口**授权，明确短暂停用 HA、临时调整并恢复 Core watchdog，以及窗口内只读原始配对资料。当前授权仅涵盖只读准备。建议预留 30 分钟，另留恢复时间；这不是已预约的时间或停机时长承诺。
2. 安排一名操作员持有宿主 SSH，另确认本地 HA OS 控制台可达。原 SSH 固定宿主公钥严格匹配；没有带外核对的既有记录不能标成带外验证。禁止为故障临时开放 Docker TCP 或撤换原公钥。
3. 刷新当前静态副本、双遍实体预检、已有日志与宿主预检。记录原条目 ID、原容器 ID/镜像/配置实际挂载、UID/GID/普通权限、实际接口 MAC、watchdog 和 boot 原值；敏感数据只进私有工单。若源条目、SDK 版本或配置已变，先重做预检，不套用本次清单。
4. 当前原机 `watchdog=true`、`boot=true`，但需要在窗口前重新读取。确认没有升级、迁移、恢复或启停任务在进行；明确取消窗口期间的外部拉起和运维任务。Docker `restart=no` 不能阻止 Supervisor 或其他所有者拉起。检查两个引擎、其他主机和外部任务；导出器只检查当前引擎及可见同 UID 宿主进程，不能承担分布式排他锁。
5. 在 HA 配置目录之外选 root 控制的持久维护目录，不用 `/tmp`、组件目录或 `.storage`。不猜 HA OS 配置路径，以 Docker 实际 `/config` 挂载为准。预留不少于 2 GiB 并确认实际目标文件系统空间：工具源大小上限 256 MiB，还需运行时、导出、私有传输副本和全新还原目录的空间。独立工作站也要有私有存储；完整配置条目含其他集成凭据。
6. 准备最终压缩包与可信外部 SHA256、预检文件、工单、恢复命令和原 watchdog 值。先做包哈希与运行时自检，成功后才允许停 Core。遇到版本、架构、完整性、权限、宿主密钥或路径不符，在在线阶段退出。

固定包：`hwhomebridge-cold-runtime-linux-x86_64.tar.gz`，SHA256：

```text
fe5732626b7e04e139c8484f863ac7529d310ad8744519818d665ca9c081c5f1
```

本轮没有向原机传入这个包。将来传包、解压、自检会写维护目录，也应纳入该次窗口授权。只把需要的稳定 `preflight.private.json` 传入维护目录，不把 HA token、SSH 私钥放到原机，也不用 Core 容器 Python 代替宿主环境。

## 人工执行顺序

以下命令仅供未来批准的窗口使用，不能把整段当脚本粘贴。每步检查返回码及实际状态；发生不符立即进入下表。所有命令输出、原始 JSON 和路径保持私有。`HA_CONFIG`、`MAINT`、`COLD` 是操作员按现场证据填写的绝对路径；输出必须全新、位于源配置之外。结束前不清理任何恢复所需材料。

### A. 在线准备，尚不停机

在独立宿主 SSH 中记录 `ha core info --raw-json`、`ha core options --help` 和指定容器的定向 `docker inspect` 结果。CLI 帮助已经只读实查支持下述选项；现场若有版本变化，重新核对。不要记录容器全部环境变量或把含凭据的输出粘贴到聊天。

在 root 私有维护目录中确认压缩包来源和外部哈希，再解压、运行 `./cold-backup self-test`。包自检通过不等于停机条件通过，也不读取生产配对 KV。另存当前在线源配置 JSON 的哈希及预检文件哈希；供停机导出检查前后一致性。

### B. 停止原所有者并确认排他

由操作员记录恢复检查单，保持 boot 原值、不升级、不重启 Supervisor 或宿主。使用 Supervisor 支持的 Core 管理入口：

```sh
ha core options --watchdog=false
ha core info --raw-json
# 确认watchdog确实为false、无在途启停/升级任务后，再执行下一步。
ha core stop
```

不使用 `--force` 绕过离线数据库迁移；不以 `docker stop` 代替监督器管理，不停止 Supervisor。按 [Supervisor 2026.09.3 的 Core 接口](https://github.com/home-assistant/supervisor/blob/2026.09.3/supervisor/api/homeassistant.py)，stop 调用原 Core 的 stop；[Core 实现](https://github.com/home-assistant/supervisor/blob/2026.09.3/supervisor/homeassistant/core.py)默认保留容器，watchdog 会处理失败/不健康事件。临时关闭 watchdog 是本方案的额外排他措施，**不是声称单独 `ha core stop` 在任何情况下都会自动重启**。

在至少 60 秒观察期间，通过宿主通道重复读取同一个容器：ID、配置挂载和镜像不变，Running/Paused/Restarting 都为 false，Pid=0，restart 为 no，StartedAt/FinishedAt 连续稳定；再次确认 watchdog=false、SSH 可用、无其他共享可写所有者。60 秒只是操作观察，导出前后仍须由导出器完整复查，不能替代外部任务排他确认。若 Core 自行恢复或记录变化，停止流程，不反复抢停。

原部署代码的普通卸载只清空回调；**永久删除条目**会删除云端子设备、调用 `exit_brg` 并清理持久化目录。窗口不得删除/重建条目，不在智慧生活解绑，不调用 `stop_hw_hilink_bridge` 或 `exit_brg`，也不通过集成重载来“测试停机”。本轮未验证原机停机行为；最终以实际窗口证据为准。

### C. 导出、验真，再恢复原运行

```sh
# 在已自检的运行时目录中执行；三项路径已人工核对。
./cold-backup export --engine docker --container homeassistant \
  --config "$HA_CONFIG" --interface enp2s1 \
  --preflight "$MAINT/preflight.private.json" --output "$COLD"
./cold-backup verify --output "$COLD"
```

检查退出码和 JSON 的 `passed`。导出包含完整组件、旧运行目录和三份注册表；离线验证 SDK 字节、原 MAC 对应的身份解码及全部子身份。实体歧义或云端子清单差异不能隐去；`adoption_ready` 继续为 false。若解码未知格式或配对资料不符，保存私有错误记录，**不尝试在线修复、重配或重建身份**。

无论备份是否成功，只要源未变、原容器保留、没有其他身份所有者，按恢复检查单启动**原容器对应的 Core**：

```sh
ha core start
```

启动前再次确认原 ID/镜像/配置挂载，防止 `start` 在原容器缺失时新建实例。等待原 HA API、原条目状态、原桥接日志恢复；只观察智慧生活已有网关/旧灯是否在线，不新发现、不配对、不发设备控制。原容器 ID 应相同、StartedAt 应更新；这与本轮全程在线时要求启动时间不变是两个不同阶段。

随后**恢复已记录的 watchdog 原值**，再读回确认。例如原值 true 才执行 `ha core options --watchdog=true`；原值 false 就保留 false。boot 始终不改。若启动失败，保留独立宿主通道和原目录，不连续重启试错；明确记录尚未恢复的 watchdog 状态，按故障表处理。

### D. 缩短停机后的离线工作

原 Core 恢复后，再通过已固定公钥的 SSH 私有传输冷备到独立 Linux 管理员环境。原始导出和本地副本都保持私有权限，在副本上重新验真，再还原到全新包装目录：

```sh
./cold-backup verify --output "$COPIED_COLD"
./cold-backup restore --output "$COPIED_COLD" --target "$NEW_RESTORE"
```

`NEW_RESTORE/config/` 仅用于离线对照原字节、UID/GID、普通权限、mtime 和空目录。禁止启动这份配置或 SDK，禁止把它覆盖到正在运行的原 HA，禁止借此增加新网关。维护窗口无需为了离线还原长期停止原 Core。导出/还原是桥接子系统备份，不是完整 HA、数据库或 HA OS 整机备份。

## 故障、撤销和恢复

| 触发点 | 处理 | 如何恢复原运行 |
| --- | --- | --- |
| 哈希、自检、独立通道、空间、预检或排他条件不通过，尚未停 Core | 取消本次窗口，不放宽保护条件 | 若已临时改 watchdog，恢复记录原值；HA 继续运行 |
| watchdog 设置失败或读回不符 | 不执行 stop；保留私有错误证据 | 读回、恢复原值；无法恢复时明确交接，不宣称完成 |
| stop 被迁移/并发任务拒绝 | 不加 force，不发额外 stop | 确认 Core 实际状态；若仍在线，恢复 watchdog 后退出 |
| stop 返回失败/超时或 SSH 中断，Core 状态不明 | 不根据返回码猜停机成功；用独立 SSH/宿主本地控制台重新核对同一个容器 | 已在线则核对健康；已停止且源完整则恢复原 Core；通道未恢复前不部署、不重建容器 |
| Core 被自动拉起或出现第二个所有者 | 停止导出，不反复抢停，处理未排除的拉起来源 | 若原 Core 已运行，只读核对；源完整、排他重新成立才可恢复设置，否则人工交接 |
| 导出因陈旧清单、格式、占用或一致性失败 | 不发布有效快照，不改源文件/身份，不覆盖输出；问题待在线重新预检 | 先确认源完整、原容器仍在且无其他所有者，再 start 原 Core，恢复 watchdog 原值 |
| 验真/离线还原失败、传输副本损坏 | 不使用失败副本、不写回原配置；保存诊断，重新私有传输或重开窗口 | 源完整时按原 Core 恢复流程；成功还原的新目录也不能自动激活 |
| 原容器消失、源被意外修改或身份资料损坏 | 停止普通恢复流程，保留现场，不调用可能新建容器的 start，不盲目覆盖整个 `core.config_entries` | 单独制定故障恢复窗口；在新目录验真还原，逐项审阅再决定写回，避免回滚其他集成的新状态 |
| start 失败或恢复后桥接报错 | 保存宿主/Core 日志、容器和源证据，不解绑或重加集成 | 使用独立通道排查；watchdog 未恢复的状态必须显式交接，不反复启动第二个引擎 |

本次冷备窗口没有安装新代码或切换后端，所以正常“回退”就是**恢复原 Core 和原 watchdog 设置**，不需要覆盖配置。未来真正部署/接管的回退需要独立计划，不能把全新目录原始还原冒充已经验收的原机回滚安装器。

## 完成条件与本轮演练边界

未来窗口完成须同时满足：有效冷备在两个私有位置验真；独立新目录原始还原及身份/子清单对照完成；原 Core、原配置条目和旧网关恢复在线；静态代码和所属配置条目未改；watchdog 恢复原值且 boot 未改；没有第二个所有者、解绑、灯光控制或自动化改动。HA 状态值可能因家庭使用或米家自动化自然变化，不把“所有状态完全相同”当成要求。

本轮独立演练使用官方 HA OS 18.3 用户空间、Ubuntu systemd 宿主、真实 Docker、合成旧身份和只运行 sleep 的所有者。覆盖成功冷备后恢复同一容器、恢复运行时在全新目录离线还原，以及陈旧预检导致导出失败后恢复同一容器。原字节和源元数据保持不变；冷备既有的运行中、自动重启、共享写入、文件占用及篡改拒绝也一并复跑。

这**没有运行真实 Supervisor stop/start，也没有启动 HA/SDK 或完整 HA OS**；不能代替生产停机、原身份云端在线或灯卡验收。Supervisor 路由和 watchdog 语义通过已安装版本的官方源码核对，CLI 能力通过原机只读 help 核对。脱敏结果见[窗口准备与演练记录](../../experiments/openharmony_bridge/evidence/production-cold-window.json)。Jev 仅把配置重放和无路由日志分为证据类型，未判定实际绑定、授权或通过；执行顺序、身份与失败收尾由 Codex 和实际测试核对。

本机工具回归 93 项和配置校验通过；新增离线复核的 7 项也在独立 Linux 通过。演练所用脚本、复核工具和测试文件与最终源码哈希一致，包仍为以上固定 SHA256；测试容器及临时挂载清理完毕。Jev 的输入和分类结果见[脱敏记录](../../experiments/openharmony_bridge/evidence/jev-cold-window-evidence.json)，不包含设备 ID、实体名、凭据或配对资料。

窗口之后仍需：审阅旧绑定冻结方案与不可用实体处理 → 原身份离线转换 → 正式父 SN/原生目录验收 → 独立环境完整接管/回退 → 再批准原机唯一网关的部署。此手册不提前放行这些阶段。
