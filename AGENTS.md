# 项目协作指南

本文件适用于整个仓库。修改前查看工作区状态，保留已有的用户改动；按当前任务读取相关目录和 Skill，不必加载全部开发日志或模型文件。

## 项目与目录

这是 KickPi K7 / RK3576 的 Linux + openvela AMP 比赛项目：cpu3 运行 openvela，其余 7 核运行 Linux，双方经 RPMsg 通信。

| 目录 | 职责 |
| --- | --- |
| `board/contest_board/` | openvela 板级配置、启动、采集与纯 C 离线唤醒引擎 |
| `board/contest_board/linux-side/` | Linux DTS、AMP 打包配置、RPMsg 麦克风内核模块 |
| `nuttx-side/` | 公共 nuttx 仓的芯片层补丁；基线与应用方法见该目录 README |
| `tools/kws/` | 唤醒词语料、训练、评测、C 导出与部署工具 |
| `linux-apps/asr-server/` | 语音识别、VAD、声纹及 WebSocket 服务 |
| `linux-apps/miloco-server/` | 摄像头拉流、VPU 解码、YOLO NPU 检测与米家设备接口 |
| `linux-apps/harness/` | 语音对话入口、配置台、本地 MCP 与运行时技能安装 |
| `skills/` | 从开发过程沉淀的可复用 Skill，与 harness 安装的运行时技能目录分开 |
| `logs/` | 真实 AI Coding 日志，随比赛作品归集提交 |

本仓不是完整 openvela 工作区。manifest 把 `board/contest_board` 映射到外层工作区的 `vendor/openvela/boards/contest2026_290_board`；`build.sh`、`nuttx/` 和 Linux SDK 不一定在当前 checkout 中。执行构建前先定位实际工作区和工具链。

## Skills 与文档维护

涉及以下任务时，先读对应 Skill，再按需读取其 `references/`：

| 任务 | 入口 |
| --- | --- |
| 离线唤醒词开发、调优、对拍与部署到 openvela | [openvela-kws-deployment](skills/openvela-kws-deployment/SKILL.md) |
| ASR/VAD 迁移到 RKNN、语音模型转换与验收 | [speech-rknn-migration](skills/speech-rknn-migration/SKILL.md) |
| YOLO 转 RKNN、量化、后处理与检测验收 | [yolo-rknn-migration](skills/yolo-rknn-migration/SKILL.md) |
| RK3576 SDK 出各分区镜像、打 update.img 整包与烧写 | [rk3576-image-build](skills/rk3576-image-build/SKILL.md) |

- 开发、训练、模型转换和部署排障流程优先维护在对应 Skill；详细命令放在它的 `references/`，可执行实现继续放在现有工具目录。
- 服务 README 维护用途、启动、运行配置、接口、模型位置和自测入口。ASR、YOLO 的 RKNN 转换教程已集中到 Skills，不再复制回服务 README。
- 移动文档后同步更新 README、脚本注释和错误提示中的引用，避免指向已删除的章节。
- 以当前执行代码、配置和本次测量为准。历史 README、注释、评测 JSON 和日志可能对应不同模型版本，不能混成当前结果。

## 实现约束

- 本项目只维护当前版本，不添加或保留历史版本的配置迁移、旧格式解析、旧接口别名和调用方式兼容；当前硬件适配、外部协议支持及故障回退按实际运行需求保留。
- openvela 侧的 KWS 是纯 C / float32 引擎，当前板配置启用 PDM 16 kHz；Linux 侧的 ASR、声纹和 YOLO 才使用 RKNN。当前 FSMN-VAD 在 RK3576 上仍走 CPU，没有 VAD RKNN 后端。
- ASR/声纹转换默认推荐 Ubuntu x86_64 的独立 Toolkit2 环境。ONNX 导出与 RKNN 转换是不同阶段，环境和资源要求见语音 Skill。YOLO 有板上转换经验，按它自己的 Skill 选择转换机。
- `kws_tables.h`、`kws_model_data.h`、`kws_golden.h` 由 `tools/kws/export_c.py` 生成，不手工修改权重和查表数据。前端或网络变更需同步处理训练数据、导出和对拍。
- 修改跨 Linux/openvela 的音频格式、RPMsg 协议、资源地址或启动握手时，同时核对两端实现。Linux 持有 GIC distributor，openvela 的从核初始化不能覆盖 Linux 的全局配置。
- 三个 Linux 服务各自使用本目录的 `pyproject.toml` 和 uv 环境；命令在相应服务目录执行。依赖变更同步维护其锁文件，RKNN 转换依赖不混进服务运行环境。
- miloco-server 的 VPU 路径依赖系统 GStreamer/GI 与匹配的 Python 环境，`uv sync` 不会安装板厂系统插件。缺模型、包或硬件时可能发生回退，要报告实际后端。

## 验证方式

按改动选择检查，结果区分通过、失败、跳过和未执行。文档修改检查相对链接、命令语法与 `git diff --check`；不为纯文档改动启动服务、下载模型或烧录设备。

| 改动范围 | 验证入口 |
| --- | --- |
| harness 技能安装 | 在 `linux-apps/harness/` 执行 `uv run python -m unittest -v tests.test_skill_install` |
| harness 音量 MCP | 在同目录执行 `uv run python -m unittest -v tests.test_volume_mcp`，使用模拟音量设备 |
| harness TTS 配置或状态事件 | 在同目录按需执行 `uv run python -m unittest -v tests.test_tts_config tests.test_state_events` |
| harness AI 日记与记忆 | 在同目录执行 `uv run python -m unittest -v tests.test_diary`，使用临时数据库与模拟 LLM |
| harness 定时任务 | 在同目录执行 `uv run python -m unittest -v tests.test_schedules`，使用临时数据库与模拟 ChatBot |
| 视频排队与丢帧 | 在 `linux-apps/miloco-server/` 执行 `uv run python -m unittest -v test_video_pipeline`；需要 GStreamer，跳过不算通过 |
| KWS 数值或模型 | 按 KWS Skill 构建同源 `kws_host` 并运行 `golden`，再按改动补流式评测和真机验证 |
| ASR/VAD 或 YOLO 模型迁移 | 按对应 Skill 比较参考模型、转换模型和目标板结果，包含输入输出与实际延迟 |
| BSP、内核模块与 AMP 配置 | 在对应 openvela/Linux SDK 中构建，并验证启动、心跳与 RPMsg 等受影响链路 |

ASR 的 `tests/asr_ws/`、`tests/asr_wsv2/` 是交互调试入口，不能以打开页面或测试发现运行了零个用例来宣称回归通过。KWS 本机金标准和视频模拟测试也不能替代真实麦克风、NPU 或开发板验收。

没有目标设备、模型或依赖时，完成能够执行的源码/离线检查，明确列出剩余验证；不要引用旧日志充当本次测试结果。

## 硬件、产物与日志

- 只有任务涉及部署或设备调试时才执行相关操作。沿用当前任务已经明确的目标和授权；首次使用部署脚本时核对其中的主机、SDK、工作区和分区，不能照用历史 IP 或 SSH 别名。
- 烧写前确认构建成功、固件是本次产物、分区容量足够并有持久备份。部署脚本的失败传播和路径限制见 KWS Skill，不能以旧文件仍存在判断构建成功。
- 保持模型、数据和固件版本可追溯；临时转换图、缓存、录音、构建产物和凭据按对应目录的忽略规则处理。已有入仓模型与生成头文件是项目资产，不为整理目录而删除。
- 比赛日志只归集真实会话，遵循 [日志说明](logs/README.md)。不要合成开发历史，也不要把凭据写进文档、提交或工具输出。
