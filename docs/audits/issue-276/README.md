# #276：运行与新版内容流程只读基线

审计日：2026-10-24，America/New_York（EDT，UTC−04:00）。部署／运行证据采集于 **19:26–19:40 UTC / 15:26–15:40 EDT**，另有 **19:49:41Z / 15:49:41 EDT** 的 controller 远端 main 读回；整理完成时间晚于这些快照，不表示此后状态持续不变。各证据精确时间、SHA、JSON 路径、命令边界及补采见 [evidence.json](evidence.json)。源码基线为 `305fda230ad91d1c8dac8ea4d34f717a44bd91fc`。证据包的结构化时间统一为 UTC `Z`；Issue／comments 快照取得时间分别依据现有本地文件 mtime（`2026-10-24T19:23:48.386440Z`／`19:23:48.681078Z`），不是 GitHub 内容更新时间。原生文件未改写，十份 SHA 仍针对各自原始 bytes。这是 G12a 的事实盘点，不是 #288 的生产全链路证明。

19:26 部署快照显示应用健康，生产 checkout 与应用声明的 SHA 一致；快照中的 `origin_main` 仅为已保留字段，原查询只留 hash、未留 query body，不能证明该时刻远端 HEAD。controller 于 `2026-10-24T19:49:41.2450708Z–19:49:41.5674260Z` 执行只读 `git ls-remote origin refs/heads/main`，exit0，原生 stdout 为 `305fda230ad91d1c8dac8ea4d34f717a44bd91fc refs/heads/main`（原文件保留 tab）。这是较晚的远端状态，不能倒填 19:26 的远端证明；该时点缺口保留在 evidence 的 AC1／未核验清单。Registry API 为 schema25、29 reports、173 articles。Chat 当前配置为 `anthropic / claude-haiku-5-5`；这只能证明所选模式，不能证明后台作者或模型调用成功。**PDF writer 容器为 created、未运行；21 个 Hermes jobs 中仅 5 个 enabled，未见启用的 T2/T3/T10，新 T1 五分钟链路也没有安装证明。** 不能据源码、历史关单或 retained queue 宣称自动闭环已接通。

## 当前环境与证据边界

公开副本将主机绝对路径及私有地址／hostname替换为一致占位符／逻辑路径；下表中的占位符不是精确路径本身。完整绝对路径只保留在外部私有原生证据中，原件未改，依现有文件 SHA256、文件名和 JSON 指针取证。`<HOST_HOME>` 表示生产账户 home，`<DOCKER_VOLUMES_DIR>` 表示宿主 volume 根，`<PRIVATE_EVIDENCE_DIR>` 表示授权交接的私有证据目录。容器挂载目的地、API 与仓库相对路径仍作为逻辑接口保留；详细映射说明见 evidence 的 `public_copy_redaction`。AC1 的精确路径核对需访问私有原件，公开占位符只展示映射关系；AC9 的当前／历史与版本时间边界不变。

生产 checkout 为 `<HOST_HOME>/climate_monitor_wiki`，clean `main`。应用镜像 `climate-monitor-wiki:pr293-app-305fda2` 正在运行且 healthy；writer 镜像 `climate-monitor-wiki:pr293-writer-305fda2` 已创建。二者 entrypoint 为 `/app/scripts/docker_entrypoint.sh`，应用运行 `uvicorn api_server:app --host 0.0.0.0 --port 8501 --no-access-log`，writer 配置命令为 `python -m scripts.run_pdf_intake_writer`。应用 Python 3.12.15；native Python 探测为 `/opt/hermes-venv/bin/python` 3.11.17、模块 `/opt/hermes-agent/hermes_cli/__init__.py`。模块可导入不等于 author/reviewer 已执行。

| 身份 | 宿主路径 → 容器路径 | 证据与限制 |
| --- | --- | --- |
| 业务 Registry | `<HOST_HOME>/climate_monitor_data/registry` → `/registry`，二容器 RW | `CLIMATE_REGISTRY_DB=/registry/article-registry.sqlite3`；不是 runtime 同名 SQLite |
| queue | `<HOST_HOME>/climate_monitor_data/pdf-intake-queue` → `/pdf-intake-queue`，二容器 RW | bundle/status 与 active/pending 指针；存在文件不等于 task-started |
| runtime | Docker volume `climate_monitor_wiki_climate_runtime`，`<DOCKER_VOLUMES_DIR>/climate_monitor_wiki_climate_runtime/_data` → app `/app/output`、`/pipeline`，writer `/pipeline` | 与业务库分开；源码默认 backup `/pipeline/pdf-intake-backups`、review root `/pipeline/acquisition-runs`，实际变量/现有备份另见证据包，不能仅凭 compose 默认值判定 |
| runtime Wiki | `<HOST_HOME>/climate_monitor_data/runtime-wiki` → `/runtime/wiki` | app RO、writer RW；prepared 与 active 分开 |
| immutable sources/Wiki | checkout `/sources`、`/wiki` → app `/app/sources`、`/app/wiki`，RO | 原件另由 Registry `original_pdf` 持久化及受控下载；未为审计上传或重新导入 |
| governed reader | volume `climate_web_listening_issue126` → app `/opt/web-listening-data`，RW | 挂载不证明目标源可读、成功抓取或独立上下文可用 |

Compose 原生标签保留完整 override 来源链，最后为 `<HOST_HOME>/climate-monitor-ops/releases/pr293-305fda2/compose.yml`，不是只加载仓库基本 yml。详见 evidence 的 `deployment.containers`，不要用旧 D: 部署包作当前部署回执。宿主 `date` 和 `<HOST_LOCALTIME_FILE>` 为 UTC，`<HOST_TIMEZONE_FILE>` 不存在。19:40:11Z 的 gateway 进程使用 `HERMES_HOME=<HOST_HOME>/.hermes`，无 `HERMES_TIMEZONE`；该 profile config SHA 为 `6c029145…`，无顶层 timezone。已安装 Hermes clean HEAD `03544a73…`：`hermes_time.py` 按环境→effective profile config（可能含 managed overlay）→server-local 解析；`cron/jobs.py:1137–1192` 按 `get_timezone() or base_time.tzinfo` 算下一次 cron，没有读取 `schedule.tz`。health 虽声明 ET，当前 next 却为 `2026-10-25T08:00:00Z`。这是有界的部署／配置时区语义差异；未执行所有 due 路径、未核实 managed overlay，不扩写成通用调度缺陷。

## 排程、真实身份与入口

已启用 jobs 是 weekly cost report、五分钟 scheduler status exporter、daily Docker cleanup、daily PDF information check、daily server health。旧 Step 与 Issue124 四时段均 disabled。**status exporter 不是 T1。** Daily PDF Information Check 的实际 cron 为 `0 9,10 * * *`，不能替代新版每五分钟 T1。其 metadata 的 `script` 为空，不能从名称推导实际可执行入口或等同仓库新版 wrapper。只有 health job 显式 `schedule.tz=America/New_York`；其他 job 的时间不能由名称推定；当前源码／配置与 next 时间的差异按上一节处理。全部 21 条脱敏原生 metadata 在 evidence 内。

源码 `climate_monitor/schedule.py:17–23,64–89` 明确 T2 每日 06:00 ET、T3 周日 08:00 ET、T1 每五分钟、T10 每十五分钟及五分钟 admission guard。`scripts/hermes_job_information_check.sh:20–53` 先要求唯一运行 writer、reader 可用及必需配置，再运行两个 bounded batches；当前 writer 未运行不能通过该 guard。本审计没有执行 preflight 或业务入口。native reviewer 必须有实际 session/execution/reviewer 身份、Hermes state DB、claim 及精确 packet；owner route 要实际 actor/task/authorization 和不同 reviewer，不能互相冒充。

以下是**源码已存在的入口与产物**，不是本审计执行记录；所有业务动作均留给对应实施任务授权后执行。

| 阶段 | 实际支持的入口 | 保留／核对对象 |
| --- | --- | --- |
| PDF preview/确认 | `POST /api/manage/pdf-intake/preview`、`POST /api/manage/pdf-intake/import` | 一次一份 PDF；confirmed + preview SHA/digest；preview 临时解析，import 只 enqueue bundle/status |
| PDF 持久化／接续 | `python -m scripts.run_pdf_intake_writer`；`PdfIntakePipeline.process_next()` | 原始 PDF bytes/hash、bundle/batch、页码/occurrence、backup、pending_review；**没有据此证明 native PDF author dispatch** |
| T2/T3 | `python scripts/run_agent_acquisition.py --scheduled-start --acquisition-kind website_rotation --rotation-root …` / `--acquisition-kind weekly_search` | frozen task binding、轮转 reservation、source/search outcome、Registry candidates、acquisition-review；T2 要 rotation root；均有 ET guard |
| T1 | `scripts/hermes_job_information_check.sh` → `scripts/check_information.py` | frozen check run/input SHA、attempt/packet、batch/retry/backoff；`--refresh-chat` 产待审候选，不是已激活 |
| T10 | `python scripts/review_pipeline.py claim/submit/activate --kind acquisition --root … --target registry-review …` | exact candidate/readable packet、native claim/events、receipt、published pointer；claim/submit 为写；`peek` **也写 verification inventory**，不作为只读探针 |
| owner 内容／日期 | `python -m climate_registry plan-owner-check / ingest-owner-check / plan-owner-review / approve-owner-check …` | v1/v2 metadata、date-field/activity scopes、source capture、两独立 actor/task、备份与精确候选；plan 与 apply 边界不同 |
| 同步／恢复 | shared writer `refresh_checks()` / `refresh_publication()`；PDF retry、managed `attach_or_resume()`；`restore-backup` | public revision、frozen Registry snapshots、intake-manifest、prepared generation、expected-generation reload 与 active pointer；恢复入口均可能写，未运行 |
| 只读验收 | `/api/health`、`/api/config`、`/api/registry/status`；SQLite `mode=ro`；直接读现存 JSON | health/config/status 已读；内容／版本／实际 Chat active 对账只按 evidence 的可见范围，不把 HTTP200 当作内容验收 |

## G1–G11 能力与连接矩阵

“部署支持”中的“镜像声明同 SHA”仅指部署 metadata，与逐文件、实际运行身份或成功路径不同。状态是本报告语义，不是程序新增枚举。每行的详细源码行号、缺口及下一责任见 evidence。

| 功能／归属 | 源码支持 | 部署支持 | 实际连接 | 本次真实运行证据／结论 |
| --- | --- | --- | --- | --- |
| G1 #277 | published/update/revision/activity、月中归一、source vs system facts；列表仍 publication/collection 排序 | app 声明同 SHA | API 可用；新动态排序／人工明确覆盖未证实 | 旧排序已实现；新模式有缺口，交 #277 |
| G2 #279 | 原件 SHA/bytes、page/content SHA、occurrence、canonical 链接与不可变历史 | app 同 SHA；writer created | queue/DB 映射确认；实际新版样本按 evidence | 已有 intake 机制保留；语义条目及新版/重复映射完善交 #279 |
| G3 #278 | content-version enrichment、候选 snapshot/receipt、日期专用 scope/CAS | 同 SHA 声明 | 新 body/full summary/change-note/human contract 未接通证明 | 当前 owner allowed fields 没有更新说明或人工日期指令协议；完整新契约缺失 |
| G4 #281 | canonical URL/alias、PDF/core reconciliation、Article→Dates provenance | app 同 SHA | 新版与不同届次的真实样本未完整核验 | 已有映射不能等同所有材料身份判断正确；交 #281 |
| G7 #282 | source-body complete summary 校验、T1/owner补全 | writer created | 未见真实新版本作者任务证明 | 字段能接收不证明生成完整新版摘要和上版对比说明；新生成链未证实 |
| G8 #280 | durable queued/imported/pending_review/indexed/activating/chat_ready/failed、retry | writer created | 无启用新 T1/T10 证明 | 没有新 ready/held/capability/人工指令延续的完整契约证据；交 #280 |
| G9 #283 | upload→durable bundle→writer import | writer 未运行 | PDF author dispatch 未证实 | 该 writer 不含 native author task/actor/heartbeat 生命周期；queued/imported/packet 不作已启动作者证据 |
| G10 #285 | native session claim/events、owner 独立 actor、exact candidate/scope、自动批准 primitive | native Python 可导入；app 同 SHA | 启用 T10/真实新审核者未证实 | 机制已实现；不能制造 PASS 或用旧候选 receipt 批新 scope |
| G5 #284 | approved-only public projection、共享事务 writer、frozen manifest、prepared/active handshake | app 运行；writer 停止 | 公开 API 可用；当前跨端版本对账见 evidence | 已实现但当前自动同步未接通证明；held 子集新契约交 #284 |
| G6 #286 | verification inventory、public revision与projection identity；API内容读取 | app 活动 | health/config/status 有当前证据 | 新正文/summary/说明/排序的 Registry 与展示双验收未完成；交 #286、最终 #288 |
| G11 #287 | bounded T1 backoff、managed attach/resume、native claim、仅同步重试、旧代防倒退 | 同 SHA 声明；writer 停止 | 当前阶段接续实际运行未核验 | 既有恢复不重做；跨新版内容 checkpoint/fairness/不确定 generation 对账交 #287 |

## 版本绑定与人工日期的边界

`article_content_versions` 以正文/markdown hash 保持不可变，enrichment 绑定 content_version 或显式 manual article_version。候选保存完整 snapshot，`registry_publication` 分别保存 latest 与 published SHA；未批新版不能当作已公开内容。PDF document、page、occurrence 和 canonical/core link 是不同身份，重复出现不自动等于新正文；Article/Dates 有独立日期 observation 与 source/content 关联。现有样本能证明到哪一步，以 evidence 的原生表和键为准。

日期专用 owner plan 冻结**当前 published 完整 scope**、baseline reviews、nondate scope hash 及 expected latest/published/visibility；它是继承和并发校验基线，不能当作新版正文。**更新说明应比较的上一已发布正文版本**是另一基线；当前未发现将两者分开并绑定完整新版 summary/change-note 的公共 schema/CLI。缺口交 #278/#282，不用日期版本伪造正文更新。

agent 来源日期查证当前要求真实 capture、literal record context、source date kind/precision、独立精确 scope review。该要求不能代替“人明确指定日期并授权适用 scope”的业务证据；当前未发现新版人工例外的专门字段/入口，故既不宣称可用，也不虚构人类例外。本仓库还保留受严格旧 receipt 绑定的 manual meeting 兼容读取（`verification.py:160–235`），不能一概将合法历史人工证据写成无来源 PASS，也不能扩大成新人工覆盖功能。

本机 `climate-article-dates/SKILL.md` 仍写 publication/collection 排序及 independently reviewed path。其 hash 在 evidence，**不是服务器已加载证据**。与新动态排序、人工明确覆盖的规则差异交 #277/#278 及运行配置责任方核对。jobs metadata 未给出当前日期／补全／发布 skill 的加载版本；本地 skill、仓库 prompt、镜像 tag 均不能替代真实 author/reviewer 上下文的加载记录。

## 展示、Chat 与下一责任

19:36:55Z 的当前磁盘元数据中，queue 的 `active.json` 与 `public-refresh.json` 可按文件名 hash 确认：保存 generation `checks-30df9eca92ac41d28c5011955f0d01cb`（身份 hash `0b40ea906f7ea07b`），激活时间记为 `2026-10-23T17:35:36.171106Z`；对应 intake manifest SHA 为 `248082eb…`。这是当前读取的旧回执／磁盘指针；没有读取其语义内容、完整 Registry revision 或实际已加载 Chat/RAG，因此不能宣称当前跨端一致。runtime 还保留 pending/partial/chat_ready 历史状态，`pdf-intake-backups` 下有 1 `.bak`、1 `.gz`、1 `.json`；存在不代表可恢复或选定备份已验证。

Registry public revision、display generation、Wiki manifest、RAG corpus、Chat active generation 必须逐层比对。writer 会冻结 snapshot/manifest，准备新代后调用带 expected generation 的 reload；reload 回包丢失时检查 active pointer，不能据本地超时断言远端旧代。应用源码同时检查 public revision 并在成功后更换 corpus/active。当前只读证据能确认的 target/actual 值逐项在 evidence；未取得的值标为 null/未核验，绝不以 `prepared` 代替 `active`。`/api/config` 没有返回完整 generation 对账字段，不能仅凭模式可用证明同步已完成。

不需要本次作新产品决定，也不需要人工拼 manifest。#277–#287 各责任方按矩阵补齐或接通后，#288 再用授权的三来源真实更新和故障恢复证明；本基线不等它们全部实施才结束。缺失的 runtime skills、真实作者/审核者、完整内容版本样本、跨端 generation 或备份可恢复性，均以 evidence 的未核验清单交接。

本次只新增本目录审计文档；没有修改业务库、应用、jobs、配置、服务、active 指针，未运行采集、作者、review、approve、reload、resume 或故障注入。验证结果：Windows Python3.11.9 `python -m pytest -q` 在收集期停止，46 errors / 19.79s（主要缺 `fcntl`，另 `tests.fixtures` 导入失败），没有测试运行／通过声明；Node v24.14.1 `node --check showcase/app.js` exit0。manager 另诊断现成 WSL Python3.12.3 未装 pytest，未安装依赖。本目录 JSON 可解析，十项验收覆盖与未核验限制见 evidence；这些不充当生产成功证据。
