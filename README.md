# KickPi K7 (RK3576) 上的 Linux + openvela 异构多核（AMP）适配

> 2026 首届 openvela AI 硬件开发者大赛 · **新硬件适配赛道** · 290 队

## 一、作品简介

把 **openvela** 移植到 **KickPi K7（Rockchip RK3576）** 开发板，并做成一个真实的
**AMP（非对称多处理）系统**：在一颗 SoC 上，让 openvela 独占一个 A 核实时运行，
与 Linux 并存共跑。

- **cpu3（Cortex-A53）独占运行 openvela**（NuttShell 交互、实时任务）
- 其余 **7 核（3×A53 + 4×A72）继续运行 Linux**
- 两个操作系统通过**共享内存 + GIC 软中断承载的 RPMsg** 双向通信，Linux 侧表现为
  标准的 `/dev/ttyRPMSG0` 字符设备
- **openvela 侧全离线唤醒词「你好，openvela」**：PDM 麦克风常听，40 维
  log-mel + DS-CNN（纯 C，仅依赖 libm）在小核上每 80ms 推理一次（实测单次
  53ms），检出后经 rpmsg 通知 Linux（`KEY_WAKEUP` input 事件）——大核可睡、
  小核常听的 AMP 语音入口。训练语料经板载扬声器→真实 PDM 麦重录做信道
  自适应，板麦录到的真人对话/底噪作负例参训（v7：真人语音误触 8%→0.4%，
  底噪 120 次/小时→0）。训练/评测/部署全管线见 [`tools/kws/`](tools/kws/)
- **Linux 侧接力做识别**：唤醒之后，Linux 的 7 个核与 **6 TOPS NPU** 承担实时语音
  识别（SenseVoiceSmall）与说话人辨认（ERes2NetV2），两个模型都转成 RKNN 跑在 NPU
  上——声纹 4589 ms → **519 ms**，识别 702 ms → **378 ms**，端到端「说完到出结果」
  约 620 ms。小核常听、大核 + NPU 出结果，见 [`linux-apps/asr-server/`](linux-apps/asr-server/)

**已上板实测通过**：openvela 在 cpu3 稳定运行（心跳精确 500ms）、Linux 7 核不受
影响、`/dev/ttyRPMSG0` 双向回显 50/50 零丢失、openvela NuttShell 在 UART5 可交互。

这是 openvela/NuttX 生态里**首个** “Linux 持有 GIC distributor + openvela 作为同构
A 核 AMP slave” 的公开适配。

## 二、选题方向

**新硬件适配** —— 为 openvela 新增 RK3576 芯片支持与 KickPi K7 板级支持，并突破
“与 Linux 共享 GIC / 与 Linux 主控 RPMsg 互通” 两大工程难点。核心价值在于：同一颗
RK3576 上，用一套硬件同时获得 **Linux 的丰富生态** 与 **openvela 的实时确定性**，
省一颗 MCU、省一套外围电路。

## 三、目录结构

```
contest2026_290_dairoot/
├── README.md                       本文（作品说明 + 复现导航）
├── LICENSE                         Apache License 2.0 全文
├── contest2026_290_dairoot.xml     repo manifest（board 注入编译树）
├── board/contest_board/            ★ 主交付：openvela 板级适配
│   ├── README.md                   技术设计 + 详细复现步骤
│   ├── configs/nsh/defconfig       板级 defconfig
│   ├── src/                        board 启动、AMP 握手、心跳、rpmsg 回显
│   ├── scripts/, include/, Kconfig
│   └── linux-side/                 Linux 侧配套文件（DTS/its/分区/defconfig）
├── nuttx-side/                     ★ nuttx 公共仓侧的 RK3576 芯片层补丁（git am）
├── tools/kws/                      ★ 离线唤醒词：训练→导出→对拍→评测→烧写全管线
├── linux-apps/                     ★ Linux 侧用户态服务（识别模型都跑 RK3576 NPU）
│   ├── asr-server/                 语音识别 + 声纹（SenseVoice / ERes2NetV2）
│   ├── miloco-server/              摄像头视频流 VPU 硬解 + yolo11n 检测 + 米家设备开关
│   └── harness/                    语音对话入口 + Web 配置台 + 本地 MCP
├── skills/                         从本项目沉淀的四份可复用开发 Skill
└── logs/                           AI Coding 日志
```

openvela **nuttx 公共仓**内新增的 RK3576 芯片层（`arch/arm64/src/rk3576`）与 GICv2
AMP-slave 补丁（`CONFIG_ARM64_GIC_SLAVE`）照 rk3588/rk3399 模板实现，因不能经
manifest `<linkfile>` 注入，以 patch 系列放在 [`nuttx-side/`](nuttx-side/)（6 个
提交、21 文件、2261 行），在 manifest 所指的 nuttx 基线上 `git am` 可直接应用。

## 四、运行方式（复现）

**完整、可照做的复现步骤在 [`board/contest_board/README.md`](board/contest_board/README.md)。** 概览：

0. **（可选）不编译，直接烧预编译固件** —— `update.img` 首刷 / `amp.img` 迭代，
   下载 [2026-09-15 固件 Release](https://github.com/open-vela/contest2026_290_dairoot/releases/tag/firmware-2026-09-15)。
   整包以 `update.img.gz` 提供，需先解压并校验；文件 SHA256、镜像来源和刷机后
   需补装的软件见 [固件交付说明](docs/firmware-2026-09-15.md)。
1. **编译 openvela 固件**
   ```bash
   # 先给公共仓 nuttx 打上 RK3576 芯片层补丁
   (cd nuttx && git am ../contest2026_290_dairoot/nuttx-side/*.patch)
   ./build.sh vendor/openvela/boards/contest2026_290_board/configs/nsh -j$(nproc)
   # 产物 nuttx/nuttx.bin，入口 0x41800000
   ```
2. **在 KickPi Linux SDK 上叠加 Linux 侧改动**（`board/contest_board/linux-side/`
   的 DTS / its / 分区表 / defconfig；这些与具体 RTOS 无关，一次性叠加即可）。
3. **打包烧写**：把 `nuttx.bin` 作为 amp 分区固件打进 `update.img` 烧录；后续只迭代
   openvela 时可只 `dd` 刷 amp 分区（约 10 秒）。
4. **验收**（Linux 终端）：
   ```bash
   cat /proc/device-tree/model            # ...KICKPI K7 Board (AMP)
   nproc                                   # 7
   sudo busybox devmem 0x47c00004          # 0x3  = openvela 心跳版本
   echo hello > /dev/ttyRPMSG0             # openvela 回显
   cat  /dev/ttyRPMSG0                     # -> hello
   # 离线唤醒（对板说「你好，openvela」，或 aplay 播放正样本）
   echo KWS_INFO > /dev/ttyRPMSG0 && head -1 /dev/ttyRPMSG0   # 引擎状态
   dmesg | grep 'wake word'                # snd_rpmsg_mic: p=0.9xx
   sudo python3 tools/kws/deploy/wake_watch.py                # KEY_WAKEUP 事件
   ```

### Linux app 服务启动

在开发板的 Linux 上运行。首次启动前，按各服务 README 安装 `uv`、在各自目录
准备依赖（`uv sync`）、模型和个人配置；miloco 的虚拟环境还需按其 README 开启
系统 GStreamer/GI 包访问。麦克风驱动见[板级 README](board/contest_board/README.md)，供电和 zram 见
[Linux 服务部署说明](skills/openvela-kws-deployment/references/linux-systemd-services.md)。
三个预转换 RKNN 模型已发布到官方仓库，下载、SHA256 和放置位置见
[RKNN 模型交付说明](docs/rknn-models-2026-09-19.md)。

**打开三个独立终端，每个终端都从 `contest2026_290_dairoot` 的上一级目录开始。**
下面的进程会一直占用当前终端；先启动摄像头与 ASR 服务，再启动 harness，保持三个
终端运行。不要把三条命令顺序粘贴到同一个终端中等待执行。

终端 1：摄像头、YOLO 检测和米家设备接口。

```bash
cd contest2026_290_dairoot/linux-apps/miloco-server && uv run web.py yolo
```

终端 2：ASR 与声纹 WebSocket 服务。

```bash
cd contest2026_290_dairoot/linux-apps/asr-server && uv run server.py
```

终端 3：语音对话入口和配置台。

```bash
cd contest2026_290_dairoot/linux-apps/harness && uv run main.py
```

| 服务 | 默认访问地址 | 配置与依赖说明 |
| --- | --- | --- |
| miloco-server | `http://<板子IP>:8180/`，摄像头和设备面板 | [服务 README](linux-apps/miloco-server/README.md) |
| asr-server | `ws://<板子IP>:8086/ws`，识别 WebSocket | [服务 README](linux-apps/asr-server/README.md) |
| harness | `http://<板子IP>:8080/`，对话配置台 | [服务 README](linux-apps/harness/README.md) |

三个服务按同机部署使用；harness 的 ASR 连接设为
`ASR_SERVER_WS_URL=ws://127.0.0.1:8086/ws`，米家 MCP 当前连接本机 `8180`。
在 harness 配置台选择正确的麦克风并保存配置。首次使用 miloco 时按终端提示完成
米家登录和摄像头选择；YOLO 模型缺失时可能只推流，需检查启动日志中的实际后端。

需要单独调试语音识别时，另见 [ASR 浏览器调试页](linux-apps/asr-server/tests/asr_ws/README.md)。
调试页入口与正式 ASR 服务默认使用同一端口 `8086`，不要同时启动。以上命令是前台
手动启动，不会自动注册三个 app 为 systemd 服务。

## 五、关键技术难点（详见 board README 与提交历史）

| 难点 | 解决 |
|---|---|
| GICv2 被 Linux 独占 distributor | 新增 `CONFIG_ARM64_GIC_SLAVE`，openvela 不初始化 distributor，只碰自有中断位 |
| OpenAMP 静态资源表与 Rockchip 主控互通 | 逐一攻克 const 只读段 / 预设 DRIVER_OK / CPUNAME 特性 / config_len 等 5 处坑 |
| openvela 早于 Linux 启动的握手时序 | 信号量门控，先等 Linux 首个 kick（vring 就绪）再 announce |
| uart_rpmsg 私有帧协议与 Linux rpmsg_tty 裸字节不兼容 | 自建裸字节回显端点，wire-compatible |
| 板上无串口可读 | 发明 “RAMLOG + Linux devmem dump” 无串口调试法定位全部问题 |
| openvela 开源版无离线唤醒引擎（media_trigger 仅留接口） | 自研纯 C log-mel+DS-CNN 引擎：Python 训练管线与 C 实现同源查表、板上金标准对拍（&#124;Δprob&#124;<1e-7），常听于 cpu3，唤醒事件走 rpmsg 变成 Linux input 事件 |
| RKNN 不支持动态 shape，而语音长度天然可变 | 定长窗口导出 + 按帧对齐切段：窗口大小直接等于耗时（5 s 窗恒定 390 ms、10 s 窗 655 ms，选大了短句反而打不过 CPU）；长句最后一窗向前对齐成满窗、重叠部分按帧丢弃，不漏字不重复 |
| 3.8 GB 内存跑不动 936 MB fp32 权重 | 本地 ASR 全面转 int8 ONNX / RKNN fp16，加载峰值 2993 MB → 1300 MB，常驻 2.0 GB |

## 六、AI Coding 使用说明

本作品的**全过程**（调研 → 芯片层移植 → GICv2 AMP-slave 补丁 → OpenAMP 资源表
逐坑调试 → 无串口内存诊断 → 协议兼容 → 交付清理）均由 **Claude Code** 结对完成：
AI 负责阅读 openvela/NuttX 源码、编写与迭代所有 C 代码、通过 ssh+devmem 远程读板载
内存做无串口诊断、逐轮定位并修复 7 个关键 bug 直至上板双向通信成功。完整对话与工具
调用记录见 [`logs/`](logs/)。

### 沉淀的开发 Skills

以下 Skill 将项目中的实现、失败经验和验收方法整理为 AI 可执行的开发流程，
每份入口均为 `SKILL.md`，详细命令与实现边界放在同目录的 `references/`。
执行项目脚本需要本仓源码；安装到其他位置后，应先定位实际项目根目录。

| Skill | 内容与验证边界 |
|---|---|
| [嵌入式离线唤醒词开发与部署到 openvela](skills/openvela-kws-deployment/SKILL.md) | 真人/重录语料、DS-CNN 训练、Python/C 数值对拍、流式评测、固件部署与真机验收 |
| [语音模型 ASR、VAD 迁移到 RKNN NPU](skills/speech-rknn-migration/SKILL.md) | 已实现的 SenseVoiceSmall ASR 迁移；FSMN-VAD 的有状态迁移与验收方法。当前项目 VAD 在 RK3576 上仍由 CPU 执行，尚无 VAD RKNN 后端 |
| [YOLO 迁移到 RKNN NPU](skills/yolo-rknn-migration/SKILL.md) | YOLO11n 输出契约、fp16/int8 转换、预处理/DFL/NMS、板上检测与视频接入 |
| [在 RK3576 Linux SDK 上编译与打包镜像](skills/rk3576-image-build/SKILL.md) | 各分区镜像来源核对、AMP 分区 FIT 打包、update.img 整包与单分区迭代、烧写与上板验收；rootfs 为预制镜像时整包不含板上手工部署的内容 |

> 提交前请把 `logs/your-github-login/` 替换为你的真实 GitHub 登录名目录，并按
> 《AI Coding 日志归集与提交手册》导出本次 Claude Code 会话的 `.jsonl` 到该目录。

## 七、许可证

除文件另有声明外，本项目原创源码采用 **Apache License 2.0**，完整条款见
[LICENSE](LICENSE)。已有文件中的版权及许可证声明继续有效，包括：

- Linux 麦克风驱动的 [snd_rpmsg_mic.c](board/contest_board/linux-side/snd-rpmsg-mic/snd_rpmsg_mic.c)
  和 [Makefile](board/contest_board/linux-side/snd-rpmsg-mic/Makefile) 保留 `GPL-2.0` 声明。
- [AMP 设备树片段](board/contest_board/linux-side/dts/rk3576-kickpi-k7-amp.dtsi)
  和 [Linux AMP 设备树](board/contest_board/linux-side/dts/rk3576-kickpi-k7-linux-amp.dts)
  保留 `GPL-2.0+ OR MIT` 双许可证声明。

第三方依赖、模型、数据及固件中包含的系统组件遵循各自的许可证；根目录
`LICENSE` 不替代这些组件的许可条款。
