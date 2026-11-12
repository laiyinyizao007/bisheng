# BISHENG 本地部署记录（Windows 11 + Docker Desktop）

部署日期：2026-10-28　版本：`v3.0.0-beta5`　访问地址：http://localhost:3001

## 1. 启停命令

必须同时带上覆盖文件，否则会回到拉不下来的原版镜像：

```bash
cd bisheng/docker
docker compose -f docker-compose.yml -f docker-compose.local.yml -p bisheng up -d     # 启动
docker compose -f docker-compose.yml -f docker-compose.local.yml -p bisheng down      # 停止（数据在 ./data，保留）
docker compose -p bisheng ps                                                           # 状态
```

compose 的 service 名是 `backend` / `backend_worker`，`bisheng-backend` 是 container_name，用错会报 `no such service`。
第一个注册的账号自动成为超级管理员。

## 2. 与官方配置的差异（`docker-compose.local.yml`）

| 项 | 官方配置 | 本地配置 | 原因 |
|---|---|---|---|
| openfga / openfga-migrate | `cr.dataelem.com/dataelement/openfga:v1.15.2-dm` | `openfga/openfga:v1.15.1` | `cr.dataelem.com` 需要登录，匿名拉取返回 401；`-dm` 版仅多了达梦 DB 支持，本地用 MySQL 不需要 |
| minio（Milvus 底层存储） | `cr.dataelem.com/minio/minio:RELEASE.2023-03-20T20-16-18Z` | `bitnamilegacy/minio:2023.3.24`（`user: root`，entrypoint 指向 `/opt/bitnami/minio/bin/minio`） | 同上；官方 `minio/minio` 被国内镜像源 403，quay.io 需登录 |
| redis 宿主机端口 | `6379` | `16379`（容器内仍是 `redis:6379`） | 宿主机 6379 被另一项目的 `gemp-redis-dev` 占用 |
| 所有对外端口 | `0.0.0.0` | 只绑 `127.0.0.1`（`ports: !override`） | invest 的 ADR `20261110-bisheng-ai-researcher` 安全约束 2：仅内网监听，不暴露 |

Code 节点：本版本 `workflow.code_node_enabled` 默认关闭且本地配置未开启（该节点无沙箱，开启即可在服务器执行任意命令），保持关闭。

未做的验证：没有申请 `cr.dataelem.com` 账号，因此未用原版镜像对比过；v1.15.1 与 v1.15.2 的差异未逐项核对。
后端日志里 OpenFGA 读取请求均为 200，权限库工作正常。

## 3. 环境改动（均有备份）

| 文件 | 改动 | 备份 |
|---|---|---|
| `~/.wslconfig` | `memory` 16GB→32GB，`processors` 8→16（官方要求 ≥32GB / ≥8 核） | `~/.wslconfig.bak-20261028` |
| `%APPDATA%\Docker\settings-store.json` | `ProxyHTTPMode` 由 `system` 改为 `manual`，`OverrideProxyHTTP(S)` 置空，即 Docker 不走代理 | `settings-store.json.bak-20261028` |
| `~/.docker/daemon.json` | 排查期间临时去掉过 `registry-mirrors`，最终已恢复原样 | `daemon.json.bak-20261028` |

Docker 不走代理的后果：拉取被限制访问的仓库（如 ghcr.io）可能失败。需要时还原 `settings-store.json` 备份并重启 Docker（需结束 `com.docker.backend` 与 `Docker Desktop` 进程并 `wsl --shutdown`，只关界面不会重启引擎）。

## 4. 排障记录

### 4.1 镜像拉取极慢（0.1 MB/s）

- 现象：`bisheng-backend`（约 6GB）、`milvus`（约 2.4GB）停在 `Pulling fs layer`，小镜像正常。
- 排查：去掉国内镜像源后升到约 5 MB/s；再让 Docker 不走代理后约 15 MB/s。结论是代理与镜像源组合对大层限速或断流。根因未做进一步验证。
- 注意：Docker Desktop 重启要结束 `com.docker.backend`，否则 `docker info` 仍显示旧配置。

### 4.2 worker 启动时两条 ERROR（可忽略）

```
Failed to initialize context 'redis':    "Context 'redis' not found. Available contexts: []"
Failed to initialize context 'database': "Context 'database' not found. Available contexts: ['redis']"
```

毕昇上下文管理器"先获取、没有就注册默认实例"的写法，用 ERROR 级别打印了提示信息。紧随其后日志为 `Registering a new instance` 和 `initialized successfully`，之后 Celery 任务正常 `succeeded`。每次 worker 启动都会出现，不是故障。

### 4.3 端口占用

宿主机 6379 被 `gemp-redis-dev` 占用，redis 容器曾因 `port is already allocated` 无法启动，已通过改映射端口解决。其余端口（3306、7860、3001、8080、9200、9100/9101、19530 等）无冲突。

## 5. 部署后状态（2026-10-28 检查）

12 个容器全部运行：backend（healthy）、backend-worker、frontend、mysql、redis、elasticsearch、etcd、minio、milvus-standalone、openfga。
`http://localhost:7860/health` 与 `http://localhost:3001/` 均返回 200。

## 6. 模型配置（MiniMax，2026-10-29）

- 服务提供方 `Minimax`（id 1），API Host：`https://api.minimaxi.com/v1`（国内站；key 在 `.env` 的 `MINIMAX_API_KEY`，不写入文档）。
  表单默认值 `https://api.minimax.com/v1` 连接失败，不要用。国际站 `api.minimax.io` 对该 key 返回 401。
- 可用 LLM 以账号接口 `GET /v1/models` 为准：M3.1-Flash、M3、M2.7(+highspeed)、M2.5(+highspeed)、M2.1(+highspeed)、M2。
- 已验证正常（`/llm/verify` status=0）：M3.1-Flash、M3、M2.7、M2.7-highspeed、M2.5-highspeed、Text-01。
- 遗留问题：`MiniMax-M2.5` 被建成了 embedding 类型，验证失败（`No embedding data received`），它是对话模型，应删除。知识库 embedding 模型尚未配置，需另选向量模型。
- 系统默认：工作流、工作台（日常/任务模式）= `MiniMax-M3.1-Flash`；助手、评测、会话标题、知识库溯源/摘要 = `MiniMax-M3`（系统在首个 LLM 添加后自动填入）。

### 登录注意

- 登录要图片验证码（`/user/get_captcha`），且**密码登录按邮箱（external_id）匹配**，用用户名 `avery` 会报"账号或密码错误"。
- 模型更新接口 `PUT /llm` 是**整体替换模型列表**，漏带的模型会被删除，更新时必须带上全部现有模型的 id。

## 7. Embedding 配置（硅基流动，2026-10-29）

- 服务提供方 `SiliconFlow`（id 2，type `silicon`），API Host：`https://api.siliconflow.cn/v1`（Key 在 `.env` 的 `SILICONFLOW_API_KEY`）。
- **国内站与国际站账号不通用**：`.cn` 的 Key 在 `.com` 返回 401，反之亦然；毕昇表单默认 Host 是 `.cn`。
- 默认 Embedding：`BAAI/bge-m3`（模型 id 9，实测维度 1024）。官方价格页标注免费；同名 `Pro/BAAI/bge-m3` 收费（¥0.07/百万 token），不要选。
- 国内站可用向量模型（实测均可调用）：bge-m3、Pro/bge-m3、bge-large-zh-v1.5、bge-large-en-v1.5（维度 1024）、Qwen3-Embedding 0.6B（1024）/4B（2560）/8B（4096）。Qwen3-Embedding 的价格未能确认。
- **建知识库前确认 Embedding 不再变更**：换模型需要对全部文档重新向量化。
- 重排序模型：`BAAI/bge-reranker-v2-m3`（模型 id 10，type `rerank`，免费，已添加并验证）；毕昇对 `silicon` 的 rerank 走 `/rerank` 接口。`Pro/` 版收费，不要选。
- 踩坑：首次配置时 Key 是国际站的，在 `.cn` 报 401，换到 `.com` 后又因余额不足报 402，最终改用国内站 Key。

## 8. 运维配置（2026-10-28）

### 备份：`./backup.sh`

- 用法：`./backup.sh`（保留最近 7 份，`KEEP=14` 调整，`WITH_ENV=0` 不备份 `.env`，`BACKUP_ROOT=...` 改位置）。
- **备份存放位置：项目外层 `C:\Dev\projects\bisheng-rs\backups\<时间戳>\`，不在 `bisheng/docker/` 里。** 原因：`bisheng/` 是官方仓库内容，更新或重新拉取时可能被整体覆盖，放在里面的备份会和被保护的东西一起消失。
- 每份备份包含：

| 文件 | 内容 |
|---|---|
| `mysql.sql.gz` | MySQL 逻辑备份（`bisheng` + `openfga` 两个库，134 张表） |
| `minio-data.tar.gz` | 对象存储文件（知识库原文件、技能包、任务产出；毕昇与 Milvus 共用这个 MinIO） |
| `own-files.tar.gz` | **我们自己加的文件**，由 `own-files.list` 清单决定（新增自有文件时往清单里加一行） |
| `official-config.tar.gz` | 官方配置的当前副本（compose、config.yaml、entrypoint、nginx、redis、mysql 配置） |
| `project-docs.tar.gz` | 外层 `docs/` 笔记 |
| `host-config.tar.gz` | 项目之外的机器级改动：`~/.wslconfig`、`~/.docker/daemon.json`、Docker 的 `settings-store.json`，及各自的 `.bak-*` 原始备份 |
| `env.backup` | `.env` 的原样副本（**含全部密钥**）。NTFS 上 `chmod 600` 基本无效，靠目录位置保护：**不要把 `backups/` 同步到云盘或发给别人** |

- **不包含**（热备份无法保证一致性，需停机冷备）：Milvus / Elasticsearch 索引数据、etcd。索引可由 MinIO 里的原文件重建（官方有"切换 Embedding 并重建知识库"功能，重建流程我没有演练过），真正不能丢的是 **MinIO 原文件 + MySQL**。
- 脚本末尾会校验：MySQL 能解压且两个库都有建库语句；`own-files.tar.gz` 含关键文件；`.env` 副本与原件一致。任何一项不通过就删除这份备份并报错。校验里不能用 `zcat | grep -q`（`pipefail` 下会让上游收到 SIGPIPE 造成误报）。
- **还原演练（2026-10-29）**：把 `own-files.tar.gz`、`project-docs.tar.gz` 解到临时目录，逐文件与原件 `cmp`：自有文件 6 个、笔记 2 个、`.env` 副本，全部一致。MySQL 的还原演练见下一条。
- MySQL 还原演练（2026-10-29）：导入一次性 MySQL 容器，bisheng 128 表 + openfga 6 表齐全，核心表行数与线上一致（等 MySQL 真正可登录再导入，初始化阶段 `mysqladmin ping` 会过早通过）。
- 还原命令：`tar -xzf <备份>/own-files.tar.gz -C bisheng/docker/`；`zcat <备份>/mysql.sql.gz | docker exec -i -e MYSQL_PWD=<root口令> bisheng-mysql mysql -uroot`。

### 哪些是我们的文件、哪些没改过

`bisheng/docker/` 里官方文件和我们的文件混在一起。**我们没有修改过任何官方文件**，只新增了：`docker-compose.local.yml`、`backup.sh`、`own-files.list`、`DEPLOY-LOCAL.md`、`artifact-system-prompt.txt`、`paddleocr-shim/`，以及不在清单里的 `.env`（密钥）。
`nginx/conf.d/default.conf` 的修改时间会随前端容器启动而变化，这是 nginx 镜像入口脚本对挂载文件执行 `sed -i` 造成的，不是我们改的。

### Docker 日志轮转

`docker-compose.local.yml` 给全部 11 个服务加了 `json-file` 日志限额：单文件 50MB、最多 3 个。官方配置没有限制，而后端控制台日志级别是 DEBUG（`config.yaml` 的 `logger_conf.level`），长期运行会持续增长。

### 端口暴露检查（未发现问题）

担心 MySQL（root/1234）、MinIO（默认口令）、ES（无认证）等对外暴露。结论见第 11 节：Docker 端口在 WSL 内确实绑定 `0.0.0.0`，但被 Hyper-V 防火墙挡住（Public/Private 入站默认 Block）。

### 访问入口

| 入口 | 地址 |
|---|---|
| 管理端（构建/知识/模型/系统/审计…） | http://localhost:3001/ |
| 用户工作台（聊天、任务模式、知识空间） | http://localhost:3001/workspace/ |
| 后端 API / 健康检查 | http://localhost:7860/health |

### 未处理的可选项

- 后端控制台日志为 DEBUG 级别（噪声大）；可把 `bisheng/config/config.yaml` 的 `logger_conf.level` 改为 INFO 后重启 backend。
- 容器重启策略是 `on-failure`，Windows 重启后需手动启动 Docker Desktop 并 `up -d`；Docker Desktop 的"随系统启动"当前也是关闭的。
- PDF 解析默认引擎 `etl4lm` 需要外部解析服务，本部署没有，PDF 回退到本地解析器：扫描件、复杂版面、公式的效果会差；图片文件（png/jpg…）依赖该引擎做 OCR，回退到本地解析会直接报不支持。

## 9. PDF/图片解析：PaddleOCR 云 API（2026-10-29）

### 为什么需要 shim

毕昇的 `paddle_ocr` 解析器发**同步**请求（`POST <url>`，body 为 `{file: base64, fileType}`，响应 `{errorCode, result.layoutParsingResults}`）。
百度星河社区的 PaddleOCR 云服务现在只提供**异步任务接口**（`POST /api/v2/ocr/jobs` 提交 → 轮询 → 下载 JSONL 结果），原来的 `/layout-parsing` 同步地址返回 404。
`paddleocr-shim/shim.py`（容器 `bisheng-paddleocr-shim`，仅标准库，约 150 行）负责把同步请求转成异步任务，毕昇无需改代码。

- 云端返回的 `prunedResult` / `markdown` 结构与毕昇解析器所需字段一致（`block_label`、`block_content`、`block_bbox`、`block_order`）。
- 令牌来自 `.env` 的 `PADDLEOCR_TOKEN`，经 compose 变量注入 shim 容器，不写入数据库配置，shim 不记录令牌。
- shim 不对宿主机发布端口，仅容器网络内可访问（`http://paddleocr-shim:8000`）。
- 模型默认 `PaddleOCR-VL-1.6`（环境变量 `PADDLEOCR_MODEL` 可改），单任务轮询超时 900 秒。

### 运行时配置（数据库 `initdb_config` 的 `knowledges` 段，保存即生效）

| 项 | 值 |
|---|---|
| `loader_provider` | `paddle_ocr`（原 `etl4lm`） |
| `paddle_ocr.url` | `http://paddleocr-shim:8000/layout-parsing` |
| `paddle_ocr.timeout` | `600`（原 60） |

修改接口：`GET /api/v1/settings` 读取全文 YAML，`POST /api/v1/settings/save {"data": "<yaml>"}` 整体替换（保存时会清缓存）。改前务必先备份，只改需要的行。

### 验证

建临时知识库，上传一张含中文标题、段落、表格的图片和一份 2 页 PDF：21 秒内两个文件都解析成功（status=2），图片里的表格被转成 Markdown 表格，2 页 PDF 正确合并；随后删除临时知识库。未验证：检索问答和重排序（尚无真实知识库内容）。

### 注意

- **文档会整份上传到百度云**，保密文件不要导入；Word/PPT/Excel/txt/md 不经过它。
- 云端限额以官网为准（不同来源数字不一致：3,000 或 20,000 页/天），PDF 超 100 页默认只处理前 100 页。
- 令牌曾贴在对话里，如有顾虑到星河社区重新生成，更新 `.env` 后 `docker compose ... up -d paddleocr-shim`。

## 10. 磁盘空间告警与 Elasticsearch 水位线（2026-10-29）

### 现象

知识库文件全部"解析失败：Connection timed out"。OCR 其实成功了，真正超时的是写入 Elasticsearch：集群状态 `red`，2 个分片未分配，索引被设为只读。

### 根因

C 盘 926GB 已用 906GB，**仅剩 19.8GB（97.9%）**。ES 默认按百分比：已用超 95% 即触发 flood stage，拒绝写入。ES 数据目录 `./data/es` 挂载自 C 盘。
C 盘上大头：Docker `docker_data.vhdx` 42GB，Ubuntu WSL `ext4.vhdx` 72GB。

### 处理

把 ES 水位线改成**绝对剩余空间**（持久化在 ES 集群状态里，即 `./data/es`）：

```
cluster.routing.allocation.disk.watermark.low         = 10gb
cluster.routing.allocation.disk.watermark.high        = 6gb
cluster.routing.allocation.disk.watermark.flood_stage = 3gb
```

并解除只读块、重试分片分配，集群恢复为 `yellow`（单节点无副本，属正常）。

### 仍需处理

- **C 盘空间依然紧张**，该设置只是避免在 19GB 剩余时误触发保护，不是增加空间。剩余低于 3GB 时 ES 仍会只读。请清理 C 盘。
- 若 `./data/es` 被清空重建，需重新执行上述设置。
- 查看：`curl localhost:9200/_cluster/health?pretty`，`curl localhost:9200/_cluster/settings?pretty`。

## 11. 调查结论汇总（2026-10-29）

### 已验证

| 项 | 结论 | 依据 |
|---|---|---|
| 备份可恢复 | 是 | 导入一次性 MySQL 容器，128+6 表、核心表行数一致 |
| 容器重启后是否自动起来 | **不会** | Docker 官方文档：`on-failure` "doesn't restart the container if the daemon restarts"。Docker Desktop 自启也关闭。重启电脑后需手动开 Docker Desktop 并 `up -d`；要自动恢复可把 restart 改为 `unless-stopped` 并开启 Docker Desktop 开机自启 |
| 端口对外暴露 | 当前被挡 | Docker 端口在 WSL 内绑定 `0.0.0.0`（镜像网络模式）。WSL 的 Hyper-V 防火墙：Public=Block、Private=Block、Domain=Allow；当前网络 Wi-Fi/Mihomo 为 Public、Tailscale 为 Private，均 Block。**若连入域网络，或按微软文档执行 `Set-NetFirewallHyperVVMSetting -DefaultInboundAction Allow` / 新增放行规则，端口将对局域网开放，届时须先改默认口令** |
| bge-m3 输入上限 | 约 8192 token | 1.1 万汉字符（≈7700 token）成功，1.5 万（≈10500）返回 400 |
| 检索 | 可用 | 3 篇主题各异的文档入库后：Milvus 向量检索第一名 4/4（含无共同词的问法），ES 关键词对精确词只命中对应文档，重排序第一名 4/4 且得分明显拉开。**样本很小，不代表大量相似文档下的排序质量** |
| 删除知识库会清理向量与索引 | 是 | 删除后 Milvus 集合、ES 索引均消失 |
| 重排模型的毕昇侧调用 | 可用 | `/llm/verify` 对 rerank 模型返回 status=0 |
| `MiniMax-Text-01` | 可调用 | 虽不在 `/v1/models` 列表里，直接调用返回正常 |
| 模型服务 `limit: 1` | 无害 | 仅 `limit_flag` 为真才判断（`llm/domain/utils.py`），当前为 false |
| 任务模式基础设施 | 就绪 | 灵思 worker 进程运行、Redis 心跳存活、队列为空；内置技能 docx/pptx/xlsx 已入库；python-pptx/docx/openpyxl、LibreOffice、PyMuPDF 俱全。技能按"无 node/npm/pdftoppm/联网"设计，这些缺失不是问题。**未实际跑过一次任务** |
| worker 启动时的两条 ERROR | 可忽略 | `core/context/manager.py:166` 在上下文未注册时先打 ERROR 再 raise，调用方随即注册默认实例并初始化成功 |
| 重排序在界面哪里选 | 工作流知识库检索节点 | 文案"检索结果重排 / 请选择重排模型"（`flow.json`） |

### 行为说明（不是故障）

- **删除知识库后 OpenFGA 里残留元组**：创建时写入 STAGE 元组（`custom_mode`/`grant`/`visible`…）和 COMMIT 元组 `permission_enabled`；删除只撤销 `permission_enabled`（权限投影账本里仅一条 `DELETE permission_enabled`，操作状态 `FINALIZED`），其余元组保留。每删一个知识库约留下十余条无效记录，不会让资源重新可访问。文档未说明这是设计还是疏漏。
- **`pymilvus` 的 `num_entities`** 只统计已落盘数据，刚写入时为 0；应使用 `query(expr="", output_fields=["count(*)"])`。
- **`permission_projection_operation` 的 operation_type**：`RESOURCE_CREATE`、`RESOURCE_DELETE`、`DEPARTMENT_MEMBERS_ADD`，排查权限问题时可查此表和 `failed_tuple`。

### 仍未能确认

- **硅基流动 Qwen3-Embedding 是否免费**：官方价格页折叠，未能读到；国内、国际站的余额接口均已下线。若要用，先小量调用后到账户页看扣费。
- **PaddleOCR 免费额度**：官网英文页写 20,000 页/天，较早文章写 3,000 页/天；单次文件大小、超额行为官网未写。
- **国际站硅基流动 Key 为何先成功后 402**：当时直接调用成功，随后余额不足，原因无法判断（Key 已替换为国内站）。
- **毕昇自身的检索编排与问答生成**：未测（两路结果融合、工作流重排节点、LLM 基于检索结果作答）。
- **任务模式能否完整跑完一个任务**：未测。

## 12. workspace（用户工作台）前置配置（2026-10-29）

- 在 `/workspace/` 新建**知识空间**需要先配置"**工作台向量模型**"（系统模型设置 → `workbench.embedding_model`）。未配置时代码直接抛 `WorkbenchEmbeddingError`（`knowledge_space_service.py:1187`）。该项与管理端"知识库默认 embedding 模型"是**两个独立设置**；已设为 `BAAI/bge-m3`（模型 id 9）。
- 工作台的语音识别（ASR）、语音合成（TTS）仍为空，用到语音输入输出前需要另配。
- 磁盘扫描（C 盘 926GB，剩 19.8GB）已识别的大项：`C:\my\` 87.9GB、WSL Ubuntu 虚拟磁盘 72GB、Docker 虚拟磁盘 42.7GB、`pagefile.sys` 33GB、`hiberfil.sys` 25.4GB、`Downloads` 7.5GB。合计约 270GB，**其余约 630GB 未能归因**（`Windows`、`Program Files`、`C:\Dev` 等目录扫描超时）。

## 13. 各 API 免费额度速查（2026-10-29 联网查询，数字会变，以官方控制台为准）

| 服务 | 毕昇里对应 | 免费额度 | 备注 |
|---|---|---|---|
| 硅基流动（国内站） | 模型服务 | 部分模型免费（bge-m3、bge-reranker-v2-m3 等，价格页确认；免费 LLM 名单常变，以控制台"免费"筛选为准）；实名送 16 元券，免费模型不耗券；免费档约 1000 RPM | 国内/国际站账号不通用 |
| 百度 PaddleOCR 云 | 经 shim 接入 | 官网英文页：2 万页/天；较早文章：3000 页/天（每模型） | 单次 PDF 超 100 页只处理前 100 页 |
| MiniMax | 模型服务 | **官方无固定新用户免费额度**，按量计费；Token Plan 为付费订阅，9/29 公告称停止新购 | 曾有限时代金券活动，已过期 |
| Tavily | 联网搜索 | 1000 credits/月，免信用卡（basic 搜索 1 credit） | 国内可达性未验证 |
| SerpAPI | 联网搜索 | 250 次/月，50 次/小时 | |
| 博查 Bocha | 联网搜索 | 无固定免费额度，有免费测试包；预付费 ¥0.036/次 | 国内直连 |
| Jina AI | 联网搜索（DeepSearch）/ 读网页 | 新 key 一次性 1000 万 token（仅限非商业；也有来源写 100 万）；搜索每次至少 1 万 token，约 1000 次 | Reader 无 key 20 RPM，有 key 500 RPM（来源间有出入） |
| Firecrawl | 抓取 | 1000 credits/月（也有来源写 500，较旧） | 超额 HTTP 402 |
| E2B | 代码执行器（E2B 模式） | 一次性 $100 额度（Hobby） | 毕昇代码执行器"本地"模式免费但无沙箱 |
| Cloudsway | 联网搜索 | **官方未公布免费额度** | |
| SearXNG（自建） | 联网搜索 | 免费、无 key、无次数限制 | 需自己部署并开启 json 格式、关闭 limiter |
| 天眼查 | 天眼查 10 个工具 | **各来源互相矛盾**（100 或 500 次/天，是否需企业认证不明） | 未核实 |
| **Bing 搜索** | bing_search | **服务已于 2025-08-11 停用** | 毕昇里该工具不可用 |
| **OpenAI DALL·E 3** | Dalle3 绘画 | **已于 2026-05-12 从 API 移除** | 毕昇该工具写死 `dall-e-3`，不可用 |
| 时间 / 计算器 / arXiv / 宏观经济数据 | 同名工具 | 零配置免费（CPI 已实测可用） | 新浪行情未测 |

## 14. 联网搜索与网页工具配置（2026-10-29）

Key 都在 `.env`（`TAVILY_API_KEY` / `BOCHA_API_KEY` / `SERPAPI_API_KEY` / `JINA_API_KEY` / `FIRECRAWL_API_KEY`）。配置通过 `POST /api/v1/tool/config {tool_id, extra}` 写入 `t_gpts_tools_type.extra`；读回时 Key 自动遮蔽为 `********`，更新带遮蔽值会保留旧 Key（`JsonFieldMasker`）。该接口要求全局超管。

| 工具类 id | 名称 | 配置 |
|---|---|---|
| 16 | 联网搜索 | `{enabled:true, type:"tavily", config:{bing,bocha,jina,serp,tavily,cloudsway,searXNG}, prompt}`。**同一时刻只启用一个引擎（`type`）**，其余引擎的 Key 只存不用；切换引擎改 `type`。SerpAPI 的 `engine` 设为 `google` |
| 10 | Firecrawl | `base_url=https://api.firecrawl.dev, timeout=30000, maxdepth=1, limit=20`。官方表单默认 `limit=100`，一次爬取最多耗 100 credits（免费 1000/月），已调低 |
| 11 | Jina AI（读网页） | `{jina_api_key}` |

### 验证（用毕昇自己的 `load_tools` 读数据库配置，在 worker 容器内执行）

| 工具 | 结果 |
|---|---|
| `web_search`（Tavily） | 3.6 秒，返回真实结果 |
| `jina_get_markdown` | 2.1 秒，抓到百度 robots.txt |
| `fire_search_scrape` | 1.1 秒，抓到 example.com |

### 各 Key 单独实测

Tavily ✅（3.0 秒）；SerpAPI ✅（**19.3 秒，较慢**）；Firecrawl ✅；Jina Reader ✅；**博查：Key 有效但 HTTP 403 "没有足够余额或套餐额度"**，需到 open.bochaai.com 领取免费包或充值后才能调用。

### 注意

- 工具的参数名各不相同：Jina、Firecrawl 用 `target_url`，不是 `url`；`web_search` 用 `query`。
- 未配置：`SEARXNG_URL`、`E2B_API_KEY`、`TIANYANCHA_API_KEY`（留空）。
- `.env` 里的 Key 与数据库里存的是两份：以后改 Key，两处都要更新（`.env` 是留底，毕昇实际读数据库）。

## 15. 排障记录：workspace 提问无返回（2026-10-29）

### 结论：MiniMax 内容审核拦截，不是毕昇或配置故障

- 无工具、普通问题：用 id 7（MiniMax-M3）和 id 6（M3.1-Flash）直接复现，5–7 秒流式返回，聊天链路正常。
- 失败的两次请求（问题"姆巴佩·怎么了"，开启联网搜索，工具 `web_search` + `bisheng_code_interpreter`）：
  1. **首次**：搜索返回大量新闻后，最后一次模型调用返回 200 并开始流式，随后被 MiniMax 中途掐断：`openai.APIError: output new_sensitive (1027)`（**输出**审核）。毕昇记 `Agent chat execution error`，前端无任何显示。
  2. **同会话再提问**：`HTTP 422 ... input new_sensitive (1026)`（**输入**审核）。请求体里的历史 assistant 消息含上一轮的推理和搜索得来的敏感内容，每次提问都会带上，**这个会话从此无法继续**。
- 直接重放请求并逐项拆分验证：去掉 tools 仍 422；去掉那条 assistant 历史就恢复；单独发"姆巴佩怎么了"等问法全部正常。即触发点是**搜索结果/模型回复里的内容**，不是问题本身。

### 处理与注意

- 该会话已被污染：**新开会话**，不要在原会话里追问。
- 开联网搜索后，搜索结果的内容不可控，任何话题都可能被国内模型 API 的审核拦截。模型厂商一侧的审核无法在毕昇里关闭。
- 毕昇在这种错误下前端无任何提示，只能看后端日志：`docker logs bisheng-backend | grep -E "new_sensitive|Agent chat execution error"`。
- 观察：M3 / M3.1-Flash 把推理过程以 `<think>…</think>` 放在 content 里，毕昇把它原样存进历史（该例中 assistant 历史 3227 字），既浪费 token，也会把敏感内容带到后续轮次。
- 该会话的第二条用户消息内容是字面的 `</think>`，来源未查明（疑似前端的重试/继续操作）。
- 验证码答案会以 INFO 级别写入后端日志（`get_captcha captcha_char=...`），削弱验证码的作用。

## 16. 回答里出现大量 `<think>` / `</think>`（2026-10-29）

### 原因

`MiniMax-M3` 默认把推理过程以 `<think>…</think>` **塞在正文（content）里**返回；毕昇的日常模式后端只认模型返回的**独立推理字段** `reasoning_content`，不处理正文里的标签，于是标签和英文推理原样显示在回答里，并被存进会话历史（浪费 token，还会把搜索来的敏感内容带进后续轮次，见第 15 节）。`MiniMax-M3.1-Flash` 一开始就走独立字段，不受影响。

直接调 MiniMax 的对照（需要推理的题目，带/不带工具结果一致）：

| 模型 | 默认 | `reasoning_split=true` |
|---|---|---|
| M3 | 正文含 `<think>`，无独立推理字段 | 正文干净，推理走 `reasoning_content` |
| M3.1-Flash | 正文干净，推理走 `reasoning_content` | 无差别 |

### 处理

给 `MiniMax-M3`（模型 id 7）加高级参数：`config.user_kwargs = {"extra_body": {"reasoning_split": true}}`（界面里在模型卡片的"高级参数"里填 JSON；毕昇把 `extra_body` 原样发给 MiniMax）。

改前后通过毕昇的 `/workstation/chat/completions` 实测同一道推理题：

| | 可见回答中 `<think>` | 独立思考事件字符 |
|---|---|---|
| M3 改前 | 1 对 | 0 |
| M3 改后 | 0 | 439 |
| M3.1-Flash 对照 | 0 | 387 |

### 注意

- 修改前产生的会话，历史里已存了带标签的回答，**不会自动清理**；那些旧会话里再追问仍会把标签带给模型。新开会话即可。
- `PUT /llm` 会整体替换模型列表，改单个模型也要带上全部现有模型的 id。
- 其他 MiniMax 模型（M2.7 / M2.5 系列 / Text-01）我没有测它们是否也内联 `<think>`，用到时再看。

## 17. 日常模式侧栏预览（Canvas / Artifacts）（2026-10-29）

### 现状

- workspace 前端**自带**一整套 LibreChat 派生的 Artifacts 机制（`components/Artifacts`）：解析消息里的 `:::artifact{identifier type title}` 容器指令，在右侧展开面板，含"预览 / 代码"标签页、代码编辑、下载、Mermaid。预览用 **Sandpack**，已打进前端镜像；本机访问其默认打包器域名（`sandpack.codesandbox.io`）实测 HTTP 200。
- 支持的类型：`text/html`、`application/vnd.react`、`application/vnd.mermaid`（另有 `application/vnd.code-html`）。React 预览预装 react、lucide-react、date-fns、一批 Radix 组件，样式用 Tailwind。
- **后端从未让模型输出这个指令**（代码里没有任何 artifact 提示词），所以日常聊天里这个面板一直不会触发。
- 任务模式的交付物有**另一套**侧栏预览（`Linsight/Artifacts`）：md、文本、图片、pdf、docx、xlsx/xls/csv 可内联预览；**pptx 不支持**（需要后端转 pdf）；doc 老格式不支持。

### 做法

管理端"日常模式配置"的 `systemPrompt` 之前是空的（`is_fallback=True`，从未保存过）。把 `docker/artifact-system-prompt.txt` 的内容写入 `systemPrompt`（`POST /api/v1/workstation/config/daily`），其余字段原样保存。后端会在其后自动补上引用规则。原配置备份在外层 `backups/workstation-daily-config-*.json`。

### 验证（通过毕昇接口，模型 M3.1-Flash，不带工具）

| 问题 | 结果 |
|---|---|
| 做一个番茄钟网页 | 输出 `:::artifact{... type="text/html"}`，围栏 + 结束行 `:::` 正确，内容为完整单文件 HTML，无 localStorage |
| 画用户登录流程图 | 输出 `type="application/vnd.mermaid"` 的产物，结构正确 |
| 两句话解释 CPI | 普通回答，无产物 |

**未验证**：前端面板是否真的在侧栏渲染出预览（没有打开浏览器看）；MiniMax-M3（id 7）是否同样遵循；React 类型产物；偶发的指令格式错误（某次检查结束行为 False，未深究）。

### 撤销

把管理端该提示词清空并保存即可恢复；或用备份 JSON 还原。

### 相关

日常模式里"联网搜索"工具是**默认勾选**的（`default_checked: true`），每次聊天都会带上搜索工具；这会增加被模型厂商内容审核拦截的概率（见第 15 节）。

## 18. 延伸阅读

- [`docs/linsight-evolution.md`](../../docs/linsight-evolution.md)：灵思（任务模式）的规划机制、已知局限、业内做法，以及单独演进灵思或引入外部规划者的路径。
