# RK3576 RKNN 模型（2026-09-19）

三个模型发布在官方比赛仓库的
[RKNN 模型 Release](https://github.com/open-vela/contest2026_290_dairoot/releases/tag/rknn-models-2026-09-19)，
作为附件下载，不进入 Git 历史。它们是从 KickPi K7 / RK3576 AMP 开发板取回的现有
部署产物，本次未重新训练或转换；也未重新打入 [2026-09-15 固件](firmware-2026-09-15.md)。

## 文件与放置位置

下表路径相对项目根目录，文件名与服务默认配置一致。

| Release 附件 | 字节数 | 放置位置 | 用途 |
| --- | ---: | --- | --- |
| [sensevoice_5s.rknn](https://github.com/open-vela/contest2026_290_dairoot/releases/download/rknn-models-2026-09-19/sensevoice_5s.rknn) | 495,780,663 | `linux-apps/asr-server/rknn_models/sensevoice_5s.rknn` | SenseVoiceSmall，5 秒窗、中文 ASR、FP16 |
| [eres2netv2_3s.rknn](https://github.com/open-vela/contest2026_290_dairoot/releases/download/rknn-models-2026-09-19/eres2netv2_3s.rknn) | 182,145,392 | `linux-apps/asr-server/rknn_models/eres2netv2_3s.rknn` | ERes2NetV2，3 秒窗、192 维声纹特征、FP16 |
| [yolo11n_int8.rknn](https://github.com/open-vela/contest2026_290_dairoot/releases/download/rknn-models-2026-09-19/yolo11n_int8.rknn) | 7,261,323 | `linux-apps/miloco-server/yolo11n_int8.rknn` | YOLO11n，640 × 640、COCO 80 类、INT8、九输出 |

合计 685,187,378 字节（约 653.45 MiB）。校验清单同时保存在
[仓库](rknn-models-2026-09-19.sha256)和 Release 的 `SHA256SUMS` 附件中。

在开发板 Ubuntu 上，从项目根目录执行以下命令。已有同名模型时先另存；
`cp -i` 会逐个询问是否覆盖。

```bash
set -e
release_url=https://github.com/open-vela/contest2026_290_dairoot/releases/download/rknn-models-2026-09-19
model_download_dir=$(mktemp -d)
for filename in sensevoice_5s.rknn eres2netv2_3s.rknn yolo11n_int8.rknn SHA256SUMS; do
  curl --fail --location --retry 3 "$release_url/$filename" -o "$model_download_dir/$filename"
done
(cd "$model_download_dir" && sha256sum -c SHA256SUMS)
mkdir -p linux-apps/asr-server/rknn_models
cp -i "$model_download_dir/sensevoice_5s.rknn" linux-apps/asr-server/rknn_models/
cp -i "$model_download_dir/eres2netv2_3s.rknn" linux-apps/asr-server/rknn_models/
cp -i "$model_download_dir/yolo11n_int8.rknn" linux-apps/miloco-server/
```

ASR 和声纹路径可用 `ASR_RKNN_PATH`、`SPEAKER_RKNN_PATH` 修改；ASR 窗长需保持
`ASR_RKNN_WINDOW_MS=5000`。按[三个 Linux app 服务启动说明](../README.md#linux-app-服务启动)
启动服务，并检查日志中的实际后端。

## 额外依赖与兼容范围

- 目标为 **RK3576 NPU**。导出与运行接口见
  [语音 RKNN 工作流](../skills/speech-rknn-migration/references/asr-project-workflow.md)和
  [YOLO RKNN 工作流](../skills/yolo-rknn-migration/references/project-workflow.md)。
  普通 Ultralytics 单输出 YOLO 文件不能直接替换当前九输出模型。
- 取回时两个服务的虚拟环境均安装 `rknn-toolkit-lite2 2.3.2`；仍需匹配的
  `librknnrt.so` 和 NPU 驱动。本次未读取已加载运行库版本，不能仅凭 Python 包版本
  认定底层运行库相同。`uv sync` 不会安装板厂 NPU 驱动或 GStreamer 插件。
- **这三个文件不是完整离线模型包。** ASR 前端仍读取
  `iic/SenseVoiceSmall-onnx` 的 `config.yaml`、`am.mvn`、`tokens.json`；
  VAD 使用 `iic/speech_fsmn_vad_zh-cn-16k-common-pytorch`，当前由 CPU 执行。
  首次联网启动可能通过 ModelScope 下载这些资源。
- 离线部署前应准备对应的 ModelScope 缓存。`MODELSCOPE_CACHE` 默认是
  `~/.cache/modelscope`；当前路径解析支持其中的
  `models/iic--SenseVoiceSmall-onnx/snapshots/master/` 或
  `hub/models/iic/SenseVoiceSmall-onnx/`，VAD 亦需对应模型目录。
  目录存在时程序不会替你补齐缺少的文件。切换 CPU 后端还需对应原始模型权重。
- 本次附件不含录音、用户声纹注册数据、账户信息或个人配置。

## 上游来源与许可证

这些转换产物保留上游模型名称和适用许可证，根目录 Apache-2.0 不替代第三方许可证。
随附件提供的 `MODEL_NOTICES.txt` 包含以下声明和许可证全文。

| 产物 | 上游来源 / 作者 | 许可证 |
| --- | --- | --- |
| SenseVoiceSmall | Alibaba / FunAudioLLM；[原始模型 iic/SenseVoiceSmall](https://www.modelscope.cn/models/iic/SenseVoiceSmall)、[官方模型卡](https://huggingface.co/FunAudioLLM/SenseVoiceSmall) | [FunASR Model Open Source License Agreement v1.1](model-licenses/FunASR-MODEL_LICENSE.txt)，模型权重许可证；不是代码的 MIT 许可证 |
| ERes2NetV2 | Alibaba / ModelScope 3D-Speaker；[iic/speech_eres2netv2w24s4ep4_sv_zh-cn_16k-common](https://www.modelscope.cn/models/iic/speech_eres2netv2w24s4ep4_sv_zh-cn_16k-common) | 模型卡声明 Apache License 2.0；[许可证副本](model-licenses/ERes2NetV2-Apache-2.0.txt) |
| YOLO11n | Ultralytics；Rockchip 的 [YOLO11 九输出 ONNX 与示例](https://github.com/airockchip/rknn_model_zoo/blob/main/examples/yolo11/README.md)、[导出源码](https://github.com/airockchip/ultralytics_yolo11) | [GNU AGPL v3](model-licenses/YOLO11-AGPL-3.0.txt)，保留 Ultralytics / Rockchip 来源 |

本项目将前两个模型导出为定长 RK3576 FP16 产物，将 Rockchip 优化版 YOLO11n
转为 RK3576 INT8 产物。转换代码、配置与过程记录见上面的两个工作流链接。
FunASR 许可证副本取自上游提交
[`58830eca4012644aac0c3218c3ccc7d98f003fda`](https://github.com/modelscope/FunASR/blob/58830eca4012644aac0c3218c3ccc7d98f003fda/MODEL_LICENSE)；
其余副本取自发布时的上游许可证文件。

## 产物追溯与本次核验

2026-09-19 从开发板项目目录取回，路径与上表一致。记录的原文件修改时间（UTC+8）：

| 文件 | 原文件修改时间 |
| --- | --- |
| `sensevoice_5s.rknn` | 2026-08-31 03:44:20 |
| `eres2netv2_3s.rknn` | 2026-08-31 00:47:17 |
| `yolo11n_int8.rknn` | 2026-09-06 18:27:09 |

修改时间不是已确认的构建时间。现有产物未附完整的上游权重 revision、转换环境锁定
和构建 manifest，因此以本次 SHA256 固定交付内容，不宣称可以逐字节重建。
本次核验范围为开发板原文件与下载副本的大小、SHA256 一致，以及 Release 附件完整性。
未重新运行模型精度、性能或服务推理测试；历史实测数据不代表本次重新验收。
