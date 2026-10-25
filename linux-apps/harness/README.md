# harness

## 启动

先启动 ASR 服务；需要摄像头和米家设备功能时，同时启动 miloco-server。
完整命令见总 README 的[三个服务启动说明](../../README.md#linux-app-服务启动)。
在本目录（`linux-apps/harness/`）执行：

```bash
cp .env.example .env   # 首次配置；已有 .env 时跳过，先填写所选 LLM / TTS 的凭据
uv sync
uv run main.py
```

[`.env.example`](.env.example) 包含 ASR 地址、LLM 和各 TTS 后端配置。对话、看摄像头和 AI 日记共用 `OPENAI_MODEL`。
`main.py` 自动读取 `.env` 并覆盖同名进程环境变量，修改后重启服务生效。

配置台位于 `http://<板子 IP>:8080`。以可访问板上麦克风和桌面音频服务的用户运行，
在配置台选择正确的麦克风；系统还需安装 `libportaudio2` 和 `pulseaudio-utils`。
同机部署时，在本目录 `.env` 中配置
`ASR_SERVER_WS_URL=ws://127.0.0.1:8086/ws`，并准备所选 LLM / TTS 后端的个人配置。

## 声纹

配置台的当前语音消息旁提供“添加录入”：选择一句至少 1 秒有效语音，输入姓名并保存。
可选择已有姓名补充样本，每人保留最近 10 个不同样本；重复录入同一句不会增加样本。
聊天区右上角“声纹”可查看姓名、样本数并删除声纹。历史会话和纯文本消息不能录入。

姓名和归一化后的 embedding 保存在 **harness 本机**的 `data/db.sqlite3`，
使用 SQLAlchemy ORM 的 `Voiceprint` 模型及 `voiceprints` 表（`name`、`embeddings`、`updated_at`），
与聊天记录共用数据库，重启后自动读取。浏览器不保存声纹；ASR 只在当前连接内存中保留预设。
数据库及当前全部表在首次保存会话或录入声纹时自动创建。

harness 在 ASR WebSocket 建连后发送 `set_speaker_profiles`，收到确认后开始上传音频。
录入、删除后会全量更新预设，断线重连后重新发送；ASR 不会把预设写入磁盘。
声纹弹窗显示同步状态，保存成功但同步失败时会提示原因。

harness 直接使用 ASR `/ws` 的 STT 消息中的 `embedding`、`speech_ms` 和 `audio_url`，
无需重复请求 `/embedding`。需使用当前的 [asr-server](../asr-server/README.md)。
只有点击录入的样本会持久化；本轮最近 200 句的临时 embedding 留在内存，重开会话即清空。
embedding 不发给浏览器或 LLM。

说话人以 ASR 返回的 `speaker` 为准，SDK 原样写入用户消息 JSON 的 `speaker`，
页面显示并随聊天记录保存。harness 不进行二次声纹匹配，也不使用本地录入样本改写该字段。
录入同步成功后，从后续语音开始由 ASR 匹配并返回姓名，既有消息保持原结果；打字消息不带 `speaker`。

依赖已发布的 `aichat-sdk>=0.1.2`，执行 `uv sync` 即可安装，无需配置 `PYTHONPATH`。
预设同步由 harness 的 `PresetAsrClient` 接入，沿用 SDK 的音频收发和 STT 调度。

配置台内部接口（均由 harness 处理）：

- `POST /api/voiceprints`：`{"name":"小明","audio_url":"消息中的录音地址"}`，从当前缓存录入。
- `DELETE /api/voiceprints`：`{"name":"小明"}`，删除该姓名全部样本。
- `GET /api/state` 和 `/api/events` 的 `status.voiceprints` 返回姓名、样本数、`sync` 同步状态及当前消息的 ASR 说话人/录入状态，不含 embedding。

离线回归（模拟 ASR/LLM，不访问真实模型、麦克风或外部服务）：

```bash
uv run python -m unittest -v tests.test_voiceprint tests.test_asr_preset_sync tests.test_state_events tests.test_session_store
```

`test_asr_preset_sync` 会监听本机随机回环端口，使用真实 ASR WebSocket 协议与匹配逻辑、模拟 embedding，
验证建连、录入、删除和重连同步，不加载模型。

## TTS 配置

TTS 引擎选项从 SDK 的 `get_tts_engines()` 动态加载，项目使用 `aichat-sdk>=0.1.2`。
当前可选豆包 v1/v2、微软 Edge 和小米 MiMo；选择后保存配置即重启生效。
MiMo 需在 `.env` 配置 `MIMO_API_KEY`，可选 `MIMO_TTS_VOICE`（默认 `mimo_default`）
和 `MIMO_TTS_INSTRUCTIONS`。

启动前会检查所选 TTS 引擎的必填项。豆包 v1/v2 缺少 `BYTEDANCE_TTS_APP_ID` 或
`BYTEDANCE_TTS_APP_KEY`、MiMo 缺少 `MIMO_API_KEY` 时，页面会明确提示 TTS 未配置及缺失项，
填写 `.env` 后需重启服务；Edge 无需这些凭据。连接失败时分别提示 ASR 或所选 TTS 引擎，
不再把所有初始化失败统一显示为“ASR / TTS 连接失败”。

`config.json` 在启动时按当前配置格式校验，TTS 名称使用当前 SDK 目录。

本地 MCP 服务脚本集中放在 `mcp/`：

- `mcp/miloco_mcp.py`：米家设备和摄像头，读取 harness 根目录的 `.env`。
- `mcp/volume_mcp.py`：本机扬声器音量。
- `mcp/schedule_mcp.py`：定时任务，调用 harness 本机的定时任务接口，见[定时任务](#定时任务)。

配置中的脚本路径相对于 harness 目录；MCP 服务列表以保存的配置为准。

## 聊天记录

会话收到结束事件（道别或空闲超时）后，`llm.messages` 中非 `system` 的消息自动保存到
`data/db.sqlite3`。点击“重新开始”、保存配置触发重启或正常退出时，
也会补存尚未保存的会话；只有系统提示词、没有聊天内容的会话不保存。
数据库及目录在首次保存时自动创建，使用 SQLAlchemy ORM 的 `ChatSession`、
`ChatMessage` 模型及事务管理。更新代码后执行 `uv sync` 安装 ORM 依赖。

`.env` 中的 `DEBUG` 默认是 `false`。设置 `DEBUG=true` 并重启服务后，
每轮会话还会在 `data/` 下保存一个 `<UTC开始时间>-<会话ID>.jsonl` 调试文件，
一行一条完整的 `llm.messages` 原始消息，包括 `system`、`tool_calls`、
`reasoning_content`、`audio_url` 等字段。同秒开始的会话也会各自保留文件。
关闭 `DEBUG` 仅停止生成新的 JSONL 文件，SQLite 聊天记录照常保存。

`sessions` 表保存唯一会话 ID、UTC 开始时间和结束时间；`messages` 表按
`session_id`、从 0 开始的连续 `position` 保存角色和正文，过滤所有 `role=system` 的消息。
`role=user` 的 `content` 为包含字符串 `text` 的 JSON 对象或 JSON 字符串时，
正文只保存 `text`，例如 `{"text":"查一下天气。","speaker":"unknown"}` 保存为 `查一下天气。`。
其他正文字符串原样保存，空值或缺省值存为 SQL NULL，结构化内容存为 JSON 文本；
正文不包含外层 `role`、`time`、`tool_calls`、`audio_url` 等字段。
token 用量保存在独立的 JSON 字段 `usage`，没有用量数据时为 SQL NULL。
发送时间保存在独立的文本字段 `time`，优先使用消息顶层 `time`，缺失时提取
`content` JSON 对象中的 `time`；保留原时间文本和时区偏移，没有时间时为 SQL NULL。
说话人保存在独立的文本字段 `speaker`，优先使用消息顶层 `speaker`，缺失时提取
`content` JSON 对象中的 `speaker`；没有说话人时为 SQL NULL。
表结构由新数据库创建，不自动迁移旧数据库。
历史摘要和详情直接使用已入库的正文文本，不再解析正文中的 JSON。
会话和消息在同一事务中写入，同一轮只保存一次。
录音文件仍由 ASR 服务管理。
该目录已在 Git 忽略规则中。

安装了 SQLite 命令行工具时，可在 harness 目录查询最近的会话：

```bash
sqlite3 data/db.sqlite3 'SELECT id, started_at, ended_at FROM sessions ORDER BY started_at DESC LIMIT 10;'
sqlite3 data/db.sqlite3 "SELECT time, speaker, content, usage FROM messages WHERE session_id = '会话ID' ORDER BY position;"
```

数据库和调试文件独立保存，写入失败会记录错误日志，不打断对话；
收到结束事件时保存失败的，会在后续重启或退出时重试已启用且尚未成功的存储。
强制杀进程或断电前尚未结束的会话不会补存。

配置台聊天区点击“历史会话”，进入独立页面 `/history`（[`history.html`](history.html)），
可分页查看已保存的会话，按消息内容或会话 ID 搜索，
并打开会话查看按顺序保存的消息、开始/结束时间和 token 用量。
搜索包含用户、助手和工具消息；留空并点击“搜索”即可刷新会话。
日期筛选按配置台时区的会话开始日期计算，与日记归属日期一致，可与关键词一起使用；
点击“全部日期”清除日期筛选。日期、关键词和页码保存在 URL 中，刷新后保留。
时间以浏览器本地时区显示，列表按开始时间从新到旧排列，每页 20 个会话。
查询直接读取 `data/db.sqlite3`，不依赖 `DEBUG` 或 JSONL 文件，也不打断当前对话。
尚未结束、尚未保存的会话仍在实时聊天区查看。

历史查询接口：

- `GET /history?date=YYYY-MM-DD`：历史会话页面，`date` 可选。
- `GET /api/sessions?q=关键词&date=YYYY-MM-DD&limit=20&offset=0`：返回 `sessions` 摘要列表、`total`、`limit` 和 `offset`。
  `q` 同时匹配会话 ID 和任意消息内容，最长 200 字符；`limit` 为 1～100，`offset` 为非负整数。
  `date` 可选，按配置台时区筛选当天开始的会话，非法日期返回 400。
- `GET /api/sessions/{session_id}`：返回 `session` 及按 `position` 排序的 `messages`，
  每条消息包含 `position`、`role`、`speaker`、`time`、`content`、`usage`。会话不存在时返回 404。

数据库尚不存在时返回空列表，不创建文件；数据库不可读时返回 503，详细原因记录在服务日志。

存储与会话生命周期测试（使用临时数据库和模拟 ChatBot，不访问音频设备或外部服务）：

```bash
uv run python -m unittest -v tests.test_session_store tests.test_session_query
```

## AI 日记与记忆

harness 启动 1 分钟后整理第一轮，之后每小时一轮：读取 `data/db.sqlite3` 中已保存、尚未整理的会话，
用 `.env` 中与对话相同的模型（`OPENAI_*`）为每天写一篇第一人称日记，并更新一份长期记忆。
新会话开始时（包括唤醒词触发的重新开始），最新记忆以 `<memory>` 块接在人设后面写进系统提示词，
可在实时聊天区展开系统提示词查看；`config.json` 中的人设不变。进行中的会话尚未保存，不会被整理，
所以记忆最多滞后一轮；需要马上生效时在日记页点「立即整理」，再开始新会话。实现见 [`diary.py`](diary.py)。

- 日期按 `config.json` 的时区划分，同一天的新会话合并进当天日记。
- 说话人姓名来自 ASR 声纹匹配的 `speaker`：已录入的姓名原样使用（`小明：…`）；
  `speaker_N` 编号只在一次连接内有效，和 `unknown`、打字消息一样写作「用户」。
  记忆按人一行，如 `- 小明：喜欢喝美式`，全家共同的写 `- 家里：…`。注入时说明按人条目对应
  消息 `speaker` 中的真实姓名，认不出说话人时不把对方当成记忆里的某个人。
  录入声纹之前的对话记在「用户」下，不会自动改到姓名下。
- 记忆只记明确说过的长期信息和带日期的近期安排；不记寒暄、一次性查询、对助手的指令，
  以及密码、验证码、证件号、银行卡号等敏感信息。对助手的长期要求请写在人设里。

配置台聊天区的「日记」链接跳到日记页 `http://<板子 IP>:8080/diary`（[`diary.html`](diary.html)）：

- 月历按周一开头显示一个月，有日记的日期着色并显示开头几行和对话次数，点日期在旁边查看全文；
  可前后翻月或点「今天」回到本月。直接打开时本月还没有日记，会跳到最近有日记的月份。
  选中的日期写在地址栏 `#YYYY-MM-DD`，刷新或收藏后仍停在这一天。
- 右侧日记信息框的“查看当天历史会话”跳到 `/history?date=YYYY-MM-DD`，直接筛选选中日期的会话；
  选中尚无日记的日期也可查看已保存的会话。
- 右侧可查看、修改或清空记忆（最多 2000 字，保存后下次会话生效）。
- 顶部「立即整理」马上整理一轮；整理状态通过配置台同一个 SSE 实时更新，后台定时整理结束后也会自动刷新月历和记忆。
- 窄屏时改为单列，月历格子只显示日期和有日记的小圆点。

整理规则：

- 没有任何用户发言的会话（误唤醒、只有道别）只打标记，不调用模型。
- 每轮最多处理 10 块（每块约 12000 字转写，单条消息最多 300 字），积压的留到下一轮。
- 模型拒绝某块（HTTP 400，例如内容审核）时跳过该块继续后面的，下一轮重试；
  网络、鉴权、超时等错误停止本轮，已完成的部分保留，下一轮重试。
- 模型调用期间在页面修改了记忆，本块放弃，下一轮以页面保存的记忆为基础重新整理。
- 未配置 `OPENAI_API_KEY` 时，只有存在待整理会话才报错；错误显示在日记页顶部，详细原因见服务日志。

harness 运行时通常不需要手动整理；需要单独跑一轮时，在本目录执行下面的命令。
它读取同一个 `.env` 和 `config.json`，与 harness 同时整理时冲突的部分留到下一轮：

```bash
uv run diary.py
```

数据保存在 `data/db.sqlite3` 的三张表，首次整理或保存记忆时自动创建，不迁移已有表：

| 表 | 内容 |
| --- | --- |
| `diaries` | `date`（本地 `YYYY-MM-DD`）、`content`、`updated_at` |
| `memory` | 单行当前记忆：`content`、`source`（`diary` 自动整理 / `manual` 页面修改）、`updated_at` |
| `diary_sessions` | 已整理的 `session_id` 及写入的日记日期；没有用户发言的会话日期为空 |

```bash
sqlite3 data/db.sqlite3 'SELECT date, content FROM diaries ORDER BY date DESC LIMIT 7;'
sqlite3 data/db.sqlite3 'SELECT content, source, updated_at FROM memory;'
```

对话转写会发给 `.env` 配置的同一个 LLM 服务；配置台没有鉴权，同网段都能查看和修改日记与记忆。

日记接口：

- `GET /diary`：日记页面。
- `GET /api/diaries?month=YYYY-MM`：返回该月的 `diaries`（按日期升序，每项含 `date`、`content`、`updated_at`、
  `session_count`），以及全部日记最早、最晚的日期 `first`、`last`（没有日记时为空串）；`month` 必填，格式不对返回 400。
- `GET /api/memory` / `PUT /api/memory`：读取或保存当前记忆 `content`、`source`、`updated_at`；
  `PUT` 的请求体为 `{"content":"..."}`，去掉首尾空白后最多 2000 字，空串即清空。
- `POST /api/diaries/update`：唤醒后台立即整理一轮，整理中返回 400。
- `GET /api/state` 和 `/api/events` 的 `status.diary`：`running`、`finished_at`、`error`、`dates`、
  `sessions`、`pending`、`skipped`。

数据库尚不存在时查询返回空结果、不创建文件；数据库不可读时返回 503，详细原因记录在服务日志。

测试（临时数据库与模拟 LLM，不访问网络、音频或真实模型）：

```bash
uv run python -m unittest -v tests.test_diary
```

## Skills 安装

配置台的 Skills 标题右侧点击“+ 安装”，支持两种方式：

- **上传安装**：选择 ZIP 技能包（含一个 `SKILL.md` 及其脚本、资源，可带外层目录），
  或直接选择 `SKILL.md`。文件最大 10 MB，ZIP 解压后最大 40 MB / 1000 个条目。
  技能安装到 SDK 的 `~/.agents/skills/<name>/`，同名技能或目录不会被覆盖。
- **命令安装**：输入单条安装命令，例如 `npx @larksuite/cli@latest install`，
  或点击“填入飞书安装命令”后执行。命令以 harness 当前用户身份、在 harness 目录运行，
  输出实时显示在页面；设备需要有 Node.js / npx 等对应工具。
  npx 的包下载确认自动同意，标准输入关闭，不支持终端交互、管道及 shell 拼接。
  命令最长运行 300 秒，超时停止整个进程组；需要交互的安装请改在终端运行。

`SKILL.md` 的 YAML frontmatter 必须包含 `name` 和非空 `description`；
上传技能的 name 限 1～64 位字母、数字、短横线、下划线，以字母或数字开头。
ZIP 上传会校验路径，拒绝目录穿越、符号链接、重复路径及加密或损坏的压缩包。
命令安装的文件位置及覆盖行为由该安装工具决定；页面完成后重新扫描 SDK 技能目录，
若没有新增技能会提示检查安装位置。安装不会自动重启当前对话；勾选所需技能，
点击“保存并重启”生效。页面保留尚未保存的配置和已有技能开关。

安装功能测试（使用临时目录及本地模拟命令，不安装真实软件）：

```bash
uv run python -m unittest -v tests.test_skill_install
```

## 扬声器音量 MCP

`mcp/volume_mcp.py` 使用 `pactl` 控制当前用户的 PulseAudio 默认输出设备。
需要系统安装 `pulseaudio-utils`，并以运行桌面音频服务的用户启动 harness。

`main.py` 启动时先把默认输出设备设到 `DEFAULT_SPEAKER_VOLUME`（80%），
所以每次重启都会覆盖上一轮对话调过的音量；`pactl` 缺失或音频服务没起来时
只记一条 warning，不影响对话启动。

“音量”服务在默认配置中启用；已有 `config.json` 不会自动补上，需要时在配置台的 MCP servers 里添加：

```json
"音量": {"command": "python", "args": ["mcp/volume_mcp.py"]}
```

服务提供一个 `speaker_volume` 工具：

| action | percent | 用途 |
| --- | --- | --- |
| `get` | 不传 | 查询当前音量、静音状态 |
| `set` | 0～100，必填 | 设置绝对音量 |
| `increase` / `decrease` | 0～100，默认 10 | 调大 / 调小指定百分点，结果限制在 0～100% |
| `mute` / `unmute` | 不传 | 静音 / 取消静音，保留原音量 |

例如可以说“音量调到 50%”“小声一点”“静音”。设置或调整百分比保留静音状态，
需要出声时使用 `unmute`。控制对象是扬声器输出，不改变麦克风增益。
工具使用参数列表调用系统命令，执行超时会终止子进程；返回的是操作后的实测状态。

测试（模拟 PulseAudio，不改变实际音量）：

```bash
uv run python -m unittest -v tests.test_volume_mcp
```

## 定时任务

到点时助手自动执行一段说明并播报，例如「每天早上八点播报天气」「十分钟后提醒我关火」。
可以直接对助手说，也可以在配置台「定时任务」分区点「+ 新建」，在「设置定时任务」弹窗里填写说明、
重复方式和时间。改动立即生效，不需要保存并重启。实现见 [`schedules.py`](schedules.py)。

- 重复方式：不重复（指定日期）、每小时（第几分）、每天、工作日（周一到周五）、每周（选星期几）。
  时间按 `config.json` 的时区解释，弹窗在时间旁显示该时区。
- 到点时，说明以一条 `【定时任务】` 开头的消息交给助手，需要时调用工具，然后用一两句话播报；
  同一时刻到点的任务合成一条消息。会话已结束时像唤醒词一样开一轮新会话，播报后可以直接接着说；
  正在对话时最多等 60 秒让这一轮说完，再接在当前会话里执行。
- harness 当时没在运行（关机、重启中）：15 分钟以内的照常补执行，更早的记为「已错过」，不再补。
  暂停后恢复或修改后，从改动的时刻往后找下一次，不补之前的时间点。
- 列表里可以暂停 / 启用、编辑、删除和「立即运行」，立即运行不影响之后的排期。
  执行失败（例如 TTS 未配置、ASR 未启动）时任务上显示原因，详细错误见服务日志。
- 历史会话里这条触发消息显示为用户消息；AI 日记把它记作「定时任务」，只有提醒、没人回话的会话不写日记。
- 最多 50 个任务，说明最多 500 字。与配置台其他接口一样没有鉴权，同网段都能查看和修改。

`mcp/schedule_mcp.py`（配置名「定时任务」）把这些操作提供给模型：`create_scheduled_task`、
`list_scheduled_tasks`、`update_scheduled_task` 和 `delete_scheduled_task`。它通过本机
`http://127.0.0.1:8080/api/schedules` 读写，与配置台是同一份数据，语音新建的任务会实时出现在页面上。
「X 分钟后」用 `in_minutes`；不重复的任务不填日期时，取这个时间下一次到来的那天（今天已过就是明天）。
默认配置已包含该服务；已有 `config.json` 不会自动补上，需要时在配置台的 MCP servers 里添加：

```json
"定时任务": {"command": "python", "args": ["mcp/schedule_mcp.py"]}
```

接口（配置台和 MCP 共用）：

- `GET /api/schedules`：返回 `tasks`、配置时区 `timezone`（如 `UTC+8`）和配置时区的今天 `today`。
  每个任务含 `id`、`title`、`prompt`、`repeat`（`once` / `hourly` / `daily` / `weekdays` / `weekly`）、
  `time`、`date`、`weekdays`、`enabled`，以及 `summary`、`next_run`、`status_text`、`last_run_text`、
  `last_status`（`done` / `missed` / `error`）和 `last_error`。
- `POST /api/schedules`：新建，例如 `{"prompt":"查询深圳今天的天气并播报","repeat":"weekdays","time":"08:00"}`；
  每周的任务加 `weekdays`（1=周一 … 7=周日），不重复的可加 `date`，或用 `in_minutes` 表示多少分钟后。
- `PATCH /api/schedules/{id}`：只改提交的字段，例如 `{"enabled": false}` 暂停。
- `DELETE /api/schedules/{id}`：删除；`POST /api/schedules/{id}/run`：马上执行一次。
- 参数不合法返回 400，任务不存在返回 404，数据库不可读返回 503；任务每次变化或执行后，
  `/api/state` 和 `/api/events` 的 `status.schedules.revision` 加一，页面据此刷新列表。

任务保存在 `data/db.sqlite3` 的 `scheduled_tasks` 表，和其他表一样在首次写入数据库时自动创建：

```bash
sqlite3 data/db.sqlite3 'SELECT id, repeat, time, date, enabled, last_run_at, last_status FROM scheduled_tasks;'
```

测试（临时数据库与模拟 ChatBot；MCP 经测试客户端调用真实接口，不访问音频或网络）：

```bash
uv run python -m unittest -v tests.test_schedules
```

## 测试

回归测试集中在 `tests/`，在本目录运行全部测试：

```bash
uv run python -m unittest discover -v
```
