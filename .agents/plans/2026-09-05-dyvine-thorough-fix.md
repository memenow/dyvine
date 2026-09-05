## Goal

对 Dyvine（FastAPI 异步 Douyin 下载服务，当前 `main@f7cdce3`）做一次性彻底修复与重构：消除全部已核实缺陷（含 1 个确定性测试错误），完成 SQLite→Postgres 共享存储迁移以解锁多副本部署，落地应用层限流，删除死代码，将根目录批量脚本纳入正式代码标准，并同步文档与 CI 门禁。

## Success Criteria

- `make lint`（ruff + mypy）、black/isort 检查全绿；`pytest --cov=src/dyvine --cov-fail-under=80` 全绿且零 error，覆盖 Python 3.12/3.13/3.14。
- 下文缺陷清单逐项关闭，每个行为变更都有对应测试，覆盖率保持 ≥80%（目标不低于当前 90%）。
- 生产运行在 Postgres 上：Alembic 迁移可重复执行且 upgrade/downgrade 往返通过；滚动更新不误杀运行中的操作；watch 订阅数据完整迁移。
- 多副本可部署：RollingUpdate、HPA 可扩容、PDB `minAvailable`、NetworkPolicy 放行 PG；副本>1 时的存储约束（R2 或 RWX）有文档与启动校验。
- 限流生效：超限返回 429 统一错误信封；探针与 `/metrics` 豁免；配置项有文档与测试。
- 脚本合并为正式包并有测试；lifecycle 死代码删除；README/`.env.example`/docs 同步；安全扫描门禁统一。

## Context And Current Facts

基线（2026-09-05，在本机实测，`main@f7cdce3`）：

- 测试：429 passed + **1 error**（`test_spawn_tracks_then_discards_completed_tasks`，单测可确定性复现：前一阶段泄漏的真实 SSL socket 在 GC 时触发 `PytestUnraisableExceptionWarning`，且 `pyproject.toml` 的忽略正则 `Exception ignored in.*ssl\.SSLSocket` 与实际文案 `Exception ignored while finalizing socket...` 不匹配）。
- 覆盖率 90.11%（门禁 80% 通过）；最低为 `services/livestreams.py` 68%。
- ruff/black/isort/mypy 全绿。本机 Python 为 3.14.6，而 CI 矩阵仅测 3.12/3.13（`requires-python >=3.12` 允许 3.14，属于未测组合）。
- 架构：4 个业务 router（users/posts/livestreams/watch）+ SQLite WAL 操作库（单写者）+ R2 归档（boto3）+ f2 SDK（0.0.1.7，`uv.lock` 锁定）。#56 新增本地保留模式，#57 新增 watch 订阅调度（轮询+断点续传）。

已核实缺陷清单（证据=代码阅读/grep/实测；`推断`表示实现时需再确认）：

| # | 缺陷 | 证据 |
|---|---|---|
| A1 | 确定性测试 error + 无效的 warning 过滤 | 实测复现；`pyproject.toml` 正则失配 |
| A2 | Python 3.14 未纳入 CI 但被允许 | coverage 头 `python 3.14.6` + `ci.yml` 矩阵 |
| B1/B2 | 两套同名 `StorageError`（core 版从未被使用；service 版游离于 `DyvineError` 之外，逃逸时丢失 error_code） | grep：core 版在 src 内零引用 |
| B3 | `get_operation` 等文档字符串误写 raise `DownloadError` | `operations.py:552-586` |
| C1 | `handle_errors` 把全部 4xx 记为 ERROR（全局 handler 对 <500 用 warning） | `decorators.py:96-101` vs `error_handlers.py:142-148` |
| C2 | `HTTPException` 路径（鉴权 401）完全不记日志 | `http_exception_handler` 无 logger 调用 |
| C3 | JSON 日志丢失全部结构化上下文字段（只输出 correlation_id） | `logging.py` `_log` 摊平 extra 但 `JSONFormatter` 只读 2 个属性；测试无端到端断言 |
| C4 | 每次 R2 上传打 3 条 INFO | `storage.py:391,414,429` |
| C5 | `/health` 的 `cpu_percent` 恒为 0.0（每次新建 Process，首调无意义） | venv 实测：首调 0.0 |
| C6 | `setup_logging` 在 lifespan 内清空 root handlers（`推断`：可能吞掉 uvicorn 日志，实现时验证） | `logging.py:118-123` |
| D1 | `watch_store.py` 约 500 行复制 `OperationStore` 的连接管理，并 import 私有成员 | 两文件对照阅读 |
| D2 | 两个 store 各持一条 writer 连接写同一 SQLite 文件，跨 store 写无互斥，SQLITE_BUSY 风险 | 代码 + sqlite.org WAL 单写者语义 |
| D3/D4 | 无 schema 版本/迁移；operations 表无删除/清理接口（无限增长） | 无 Alembic；`operations.py` 无 delete/purge 方法 |
| D5 | SQLite 单写者锁定单副本（HPA 1:1、PDB maxUnavailable 0、Recreate） | k8s manifests |
| E1 | 死配置：`secret_key`（只校验不用）、`access_token_expire_minutes`、`rate_limit_per_second`（零消费） | 全仓搜索仅定义处命中 |
| E2 | R2 endpoint 的 `{account_id}` 模板约定隐式（缺占位符时静默 no-op） | `storage.py:158` + k8s configmap 注释 |
| F1 | `sanitize_filename` 剥离全部非 ASCII，中文标题变 `untitled`（用作 f2 filename_filter） | `users.py:115-129,415` |
| F2 | page_token 损坏静默回退到 0（掩盖客户端 bug，可致重复工作） | `routers/posts.py:209-225` |
| F3 | `get_post_detail` 用本地时区解析无时区上游时间（部署 TZ 相关） | `services/posts.py:163-173` |
| F4 | R2 上传单 PUT（大直播录制无分片/续传） | `storage.py:456-484` |
| F5 | UGC key 长度无界（S3 上限 1024 字节） | `storage.py:207-266` |
| F6 | watch loop 崩溃后直到重启才恢复（无自愈） | `services/watch.py:348-357` |
| F8 | 跨进程/副本并发建订阅时 UNIQUE 冲突以 500 逃逸（只防了同进程） | `watch.py:150-174` 无 IntegrityError 处理 |
| F9 | 后台 drain 30s > uvicorn 优雅停机 25s（停机可被 SIGKILL 打断） | `background.py:43` + Dockerfile CMD |
| F10 | R2 上传直方图桶上限 10s，大文件不可观测 | `storage.py:62-66` |
| F11 | 上传 metrics 取 `metadata["category"]`，缺 key 时 KeyError 掩盖真实错误 | `storage.py:425,448` |
| G1 | 两脚本重复实现 env 解析、硬编码 `/api/v1`、串行脚本 import 期副作用、无测试、放根目录 | 两脚本全文 + grep |
| H1 | `LifecycleManager` 及配套（测试/JSON/审计线程池）从未接入运行 | 模块 docstring + `dependencies.py` 注释 |
| I1 | README 要求同步的 AGENTS.md/CLAUDE.md 根本不存在 | `ls` 确认缺失 |
| J1 | `security.yml` 用 SARIF+exit 1 且无 trivyignores，与 `ci.yml` 注释记载的 SARIF 丢弃 ignore 行为矛盾（周扫会被已接受 CVE 搞红） | 两 workflow 对照 |
| J3 | CI 用 `mypy src/` 而 Makefile 用 `mypy src/dyvine` | 两文件对照 |
| K3 | 多副本下 watch 会在每个副本各跑一份（重复下载）；滚动更新时新 Pod 的 boot sweep 会误杀旧 Pod 在飞的操作 | `watch.resume_persisted` + `mark_incomplete_operations_failed` 无 owner 概念 |
| K5 | 多副本 + 本地保留：RWO PVC 无法共享各 Pod 落盘文件 | Deployment mounts + RWO |
| L1 | 生产用 `src.dyvine.main:app`、测试用 `dyvine.*` 双导入路径，模块级 Prometheus 指标有重复注册隐患 | Dockerfile CMD + `conftest.py` 注释 |

## Constraints And Non-goals

用户已定约束：含架构大改的一次性重构；脚本纳入正式标准；lifecycle 删死代码。

- 保持外部 API 路由与错误信封兼容；允许的契约变更仅：page_token 损坏→422、文件名保留中文、删除死配置项（`secret_key` 等）、/health CPU 语义修正。`task_id`/`downloaded_items` 别名保留。
- Non-goals：替换/fork f2 SDK；分布式全局限流；前端；实际供给 RWX 卷或 PG 实例（只做到 manifests + 文档 + 校验）；性能基准测试；新建 AGENTS.md；Dockerfile 的 black 清理 hack（接受现状+保留 CI 断言）。
- 假设（实现前可在 Open Questions 确认）：生产 PG 为与 GKE 同项目的托管 Postgres；允许一次低峰迁移窗口。

## Key Decisions

1. **共享存储选 Postgres + SQLAlchemy 2.0 异步 + asyncpg + Alembic。** 官方 asyncio 文档确认 `postgresql+asyncpg`、`async_sessionmaker`、`expire_on_commit=False`、`run_sync` 模式；Alembic 自带 autogenerate。拒绝：继续 SQLite（WAL 单写者且要求同机，见 sqlite.org）、其他关系库（团队无现有资产，asyncpg 生态更贴合 FastAPI 异步）、KV 作主存（operations/watch 需关系查询与持久化）。
2. **迁移策略：仓库抽象 + 一次性切换，不长期双写。** 先抽 `OperationRepository`/`WatchRepository` 协议（保持服务层 API 不变），再实现 PG 版 + Alembic 基线迁移 + `watch_subscriptions` 导出/导入脚本；operations 表为瞬时状态不迁移（窗口内在飞操作按失败处理并公告）。拒绝长期双写（复杂度与一致性陷阱远超收益）。
3. **滚动安全：owner_id + heartbeat + 只 sweep 孤儿。** 每行操作记录 owner（pod/replica id）与心跳；boot sweep 只失败心跳过期 owner 的 pending/running 行。拒绝：沿用全量 sweep（多副本下误杀）、分布式锁（YAGNI）。
4. **多副本 watch：API 与调度器分开部署 + `WATCH_ENABLED` 开关。** API Deployment（N 副本，调度关闭）+ watcher Deployment（1 副本，只跑 resume，不接流量）。拒绝：PG advisory lease（首期复杂度过高，可作后续项）。
5. **副本>1 的存储约束：要求 R2；无 R2 多副本需 RWX（文档+启动校验，不实际供给存储）。** 单副本无 R2 保持现状可用。
6. **限流自研小中间件，不用 slowapi。** slowapi 官方文档自述 alpha 且要求每个 endpoint 显式声明 `request: Request`（侵入全部路由）。自研：ASGI 中间件 token bucket，按 API key（无 key 时按 IP），复活 `API_RATE_LIMIT_PER_SECOND` 为每 key rps + burst 设置；超限走现有 `RateLimitError`→429 信封；探针与 `/metrics` 豁免；多副本语义为 per-replica（文档注明）。
7. **异常统一：只保留 core `StorageError(DyvineError)`，删除 service 层同名类**，存储调用方改为抛/接 core 版；修正 B3 文档字符串。
8. **日志：JSON 输出全部结构化上下文字段；4xx 降为 warning；鉴权失败记 warning；R2 上传合并为 1 条；先验证 C6 再定 setup_logging 改法。**
9. **文件名：Unicode 感知清洗**（保留 CJK 与 emoji，先做 Unicode 规范化，只剔除控制字符、保留字符 `<>:"/\|?*`、首尾点空格），行为变更记入发布说明。
10. **page_token 损坏→422**（服务端签发的 opaque token，损坏即 fail-fast；替代静默回 0）。
11. **上游无时区时间按 UTC 解析**（`get_post_detail`），消除部署 TZ 相关性。
12. **R2 上传改 `upload_file`（自动分片）**；UGC key 截断到 900 字节内；metrics category 用 `.get` 兜底；直方图桶扩展到 300s。
13. **watch 循环加 supervisor**（崩溃 bounded 重试 + 告警日志，保留“删订阅即停”语义）；并发建订阅的 UNIQUE 冲突收敛为幂等返回（F8）。
14. **drain_timeout 30s→20s**，形成 20<25<30（drain<uvicorn 优雅停机<k8s 默认宽限）链条。
15. **删除死配置**：`secret_key`（及 validator 相关分支、deploy workflow 与文档）、`access_token_expire_minutes`；`rate_limit_per_second` 复活为限流配置。
16. **脚本合并为 `scripts/dyvine_batch/` 包**：共享 API client（env 解析、前缀来自服务端 `/` 或参数，默认 `/api/v1`）、串行/并发两种模式保留、消除 import 期副作用、补单测、中文操作文案保留（团队工具定位）。
17. **删除 lifecycle**：实现+测试+JSON+`audit_executor` 预留+文档引用。
18. **统一导入路径为 `dyvine.*`**：镜像内安装项目包，Docker CMD/README/Makefile 改 `dyvine.main:app`，加回归测试防双注册。
19. **`/health` CPU**：lifespan 内 prime 一次，请求期复用同一 Process 对象非阻塞采样。
20. **CI**：矩阵加 3.14；根治 A1（找到 import 期建连者并正确关闭）+ 修正 warning 过滤；`security.yml` 与 `ci.yml` 统一为 table 门禁 + SARIF 纯观测；mypy 路径统一 `src/dyvine`；PG 集成测试用 testcontainers（官方 PostgresContainer 模式，CI 必跑）。
21. **文档**：README 去掉 AGENTS.md/CLAUDE.md 同步要求（文件不存在）；README/`.env.example`/`docs/index.html` 随本次全部变更同步。
22. **操作记录保留**：新增 `OPERATION_RETENTION_DAYS`（默认 30），启动时清理终态过期行 + PG 索引 `(status, updated_at)`。

## Recommended Approach

按“先止血、再迁移、后放大”的顺序分 9 个阶段，阶段间以前一阶段全绿为门。PG 相关阶段（2-4）建议独立分支联调后再合入；其余每 1-2 阶段一切片 PR。行为变更一律先补回归测试再改实现。迁移窗口安排在阶段 4 上线时（低峰、备份先行）。

## Work Plan

### 阶段 0 — 基线回归测试（先行，不改实现）
- 为 A1/C3/C5/F1/F2/F3/L1 各补一个失败态回归测试（红测），锁定当前坏行为。
- 依赖：无。产出：新增测试全部按预期失败。

### 阶段 1 — 阻塞性缺陷与行为修正（A1,A2,B1-B3,C1-C5,E1,E2,F1-F5,F9-F11,J3）
- A1：定位 import/collection 期创建 SSL socket 的模块（怀疑 f2 import 链或 handler 构造），改为显式关闭/懒加载；修正 `pyproject.toml` warning 过滤为实际文案（保留 `-W error`）。
- A2：CI 矩阵加 `3.14`（`requires-python` 已允许）；pin 本地 uv 与 CI 一致或放宽 pin 并记录。
- B1/B2：删除 `services/storage.py` 的 `StorageError`，统一抛 core 版；修正 B3 文档字符串；补异常映射测试。
- C1/C2：`handle_errors` 按 status 分级（<500 warning），`http_exception_handler` 加 401/4xx warning 日志；补日志断言测试。
- C3：`JSONFormatter` 输出全部扁平化上下文字段（白名单保留 keys，防 LogRecord 保留字冲突）；补端到端测试。C6：验证 uvicorn 日志是否被 setup_logging 吞掉，有则改增量配置。
- C4/C5：R2 上传日志合并为 1 条；`/health` CPU 改 lifespan prime + 复用 Process。
- E1：删除 `secret_key`/`access_token_expire_minutes` 及 validator 分支，同步 deploy workflow、`docs`、`.env.example`；E2：R2 endpoint 缺 `{account_id}` 占位符时启动期显式报错。
- F1/F2/F3：Unicode 文件名清洗；page_token 损坏→422；上游时间按 UTC 解析。各补单测。
- F4/F5/F10/F11：`upload_file` 分片上传；UGC key 截断 900B；桶扩展到 300s；category `.get` 兜底。补单测。
- F9：`drain_timeout`→20s。J3：mypy 路径统一 `src/dyvine`。
- 依赖：阶段 0。建议 PR 切片：阶段 0+1 一个 PR。

### 阶段 2 — 仓库抽象 + PG 实现 + Alembic（D1-D4,K1,K3 部分，F8）
- 新建 `src/dyvine/db/`：SQLAlchemy 2.0 models（operations、watch_subscriptions，含 `owner_id`、`heartbeat_at`）、`create_async_engine（postgresql+asyncpg）` + `async_sessionmaker（expire_on_commit=False）`。
- 新建 `OperationRepository`/`WatchRepository` 协议；PG 实现完整 CRUD + `sweep_orphans`（只扫心跳过期 owner）+ 启动期终态过期清理（`OPERATION_RETENTION_DAYS`，默认 30）+ `(status, updated_at)` 索引。
- Alembic 初始化（async env，`run_sync` 跑迁移）+ 基线 revision；`alembic check` 进 CI。
- 服务层改依赖协议（API 不变）；`create_subscription` 捕获 PG 唯一冲突收敛为幂等返回（F8）。
- 删除 `OperationStore`/`WatchSubscriptionStore` 及 sqlite 执行器、`set_executor` plumbing、`operation_db_path` 设置、双 writer 隐患（D1/D2/D3/D4 关闭）。
- 测试：testcontainers PostgresContainer 跑 PG 集成测试（CRUD、并发建订阅幂等、sweep 只杀孤儿、保留清理、upgrade/downgrade 往返）；原 SQLite 单测替换为协议级 + PG 级。
- 依赖：阶段 1。建议 PR 切片：独立分支，阶段 2+3 联调后合入。

### 阶段 3 — 数据迁移脚本与演练（K1）
- `scripts/migrate_watch_to_pg.py`：从 SQLite 导出 `watch_subscriptions`（JSON 校验）→ 导入 PG（幂等，可重跑）；operations 不迁（窗口内在飞按失败处理，写进发布公告）。
- 演练：本地 sqlite 快照 → scratch PG → 校验行数/UNIQUE/断点完整 → 回滚演练（旧镜像 + PVC 快照）。
- 依赖：阶段 2（同分支）。

### 阶段 4 — 多副本部署（D5,K3,K5,L1）
- L1：镜像安装项目包，CMD/README/Makefile 切 `dyvine.main:app`，加双注册回归测试。
- K8s：`DATABASE_URL` Secret（deploy workflow 注入）、迁移 Job（pre-deploy 跑 `alembic upgrade head`）、Deployment 切 RollingUpdate、HPA 上限>1（首期 max 3）、PDB 切 `minAvailable: 1`、NetworkPolicy 放行 PG egress、kustomize build 校验。
- `WATCH_ENABLED` 开关 + watcher Deployment（1 副本，不接流量，只跑 resume；API 副本关闭调度）。
- 副本>1 存储约束：R2 未配且无 RWX 时启动失败并给出明确错误；文档写清矩阵（单副本无 R2 可用；多副本需 R2 或 RWX）。
- 上线：低峰窗口执行迁移脚本→部署→验证探针/订阅恢复/操作流→观察一轮 HPA。
- 依赖：阶段 3。建议 PR 切片：与阶段 2/3 同分支合并后单独发版。

### 阶段 5 — 应用层限流（K2，复活 E1 rate_limit）
- ASGI 中间件 token bucket：key=API key（无则 IP），设置 `API_RATE_LIMIT_PER_SECOND`（rps）+ `API_RATE_LIMIT_BURST`；超限抛 `RateLimitError`→429 统一信封，带 `Retry-After`；探针 + `/metrics` + `/` 豁免。
- 并发正确性单测（桶行为、豁免路径、信封 shape）；文档注明 per-replica 语义。
- 依赖：阶段 1（可与 2-4 并行开发，合入在后）。

### 阶段 6 — watch 自愈（F6）
- supervisor：loop 异常 bounded 指数退避重启（上限后停服并 error 日志），删除订阅仍立即停；补单测。
- 依赖：阶段 2（PG 版 watch_store 语义）。

### 阶段 7 — 脚本标准化 + 死代码删除（G1,H1）
- 新建 `scripts/dyvine_batch/`：共享 client（env 解析一次、API 前缀可配默认 `/api/v1`）、`serial`/`concurrent` 两种命令、零 import 副作用、单测（mock httpx）、README 小节。
- 删除 `batch_download.py`、`download_serial.py`（根目录）、`services/lifecycle.py` + 测试 + `storage_lifecycle.json` + `audit_executor` 预留 + 文档引用。
- 依赖：阶段 1。建议 PR 切片：独立一个 PR。

### 阶段 8 — CI 与文档收尾（I1,I2,J1）
- `security.yml` 与 `ci.yml` 统一（table 门禁 + SARIF 纯观测 + trivyignores）；README 去 AGENTS.md/CLAUDE.md 引用；README/`.env.example`/`docs/index.html` 全量同步本次变更（PG、限流、WATCH_ENABLED、保留策略、脚本用法、契约变更）。
- 全量验证（见 Validation Plan）+ 发布说明（含迁移步骤、契约变更、回滚）。
- 依赖：全部前序。

## Validation Plan

- 每阶段门：`uv run ruff check .`、`uv run black --check .`、`uv run isort --check-only .`、`uv run mypy src/dyvine`、`uv run pytest --cov=src/dyvine --cov-fail-under=80` 全绿。
- 阶段 1：A1 单测在 3.12/3.13/3.14 均绿；C3 端到端断言 JSON 含 `user_id` 等字段；F1 断言中文标题保留；F2 损坏 token 返回 422。
- 阶段 2：`alembic upgrade head && alembic downgrade -1 && alembic upgrade head` 在 scratch PG 往返通过；testcontainers 集成测试通过；并发建订阅测试（多任务同时 create 同 user）只产生一行且返回幂等。
- 阶段 3：迁移脚本对生产快照拷贝 dry-run 行数一致；重跑幂等。
- 阶段 4：`kustomize build k8s/overlays/production` 成功；`docker build` + Trivy table 门禁通过；本地 docker 起 PG + 2 API + 1 watcher，验证：建订阅只跑一份 loop、杀一个 API 副本在飞操作不被 sweep 误杀（心跳有效）、滚动后恢复。
- 阶段 5：压测超限返回 429 + `Retry-After`；探针不限流。
- 阶段 6：注入 loop 异常验证 bounded 重启与上限停服；删订阅后确认无重启。
- 阶段 7：`python -m scripts.dyvine_batch --help` 可用；mock 单测通过。
- 最高风险验证：阶段 4 的多进程冒烟（sweep/owner/心跳正确性）——这是多副本正确性的唯一实际证据，必须亲眼通过。

## Risks / Rollback

- PG 切换数据丢失：仅 operations 瞬时行不迁移（窗口内在飞失败）；watch 行经脚本迁移并校验行数。回滚=旧镜像 + SQLite PVC 快照恢复（快照在窗口前打），PG 写入窗口数据丢弃（已公告）。
- f2 上游脆弱（pin 旧 dep、black 污染、私有 `_to_dict`）：本次不动 f2；Dockerfile hack 与 CI 断言保留。
- 文件名行为变更（中文保留）导致 R2 key 变化：属预期改进，发布说明注明；key 内超长截断保证 <900B。
- 限流 per-replica 语义：N 副本≈N 倍配额，文档注明；需全局精确限流时后续上分布式方案（非本期）。
- slowapi 被拒：如未来其脱离 alpha，可重评估替换自研中间件（接口隔离在单一模块内）。

## Open Questions

1. 生产 PG 托管选型确认（假设：与 GKE 同项目的托管 Postgres；实现前请确认，回答不影响阶段 0-2 开工）。
2. 迁移低峰窗口时间（假设可安排 30 分钟；不影响开工）。

## Sources

- https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html
- https://alembic.sqlalchemy.org/en/latest/autogenerate.html
- https://www.sqlite.org/wal.html
- https://www.sqlite.org/lockingv3.html
- https://slowapi.readthedocs.io/
- https://testcontainers-python.readthedocs.io/en/latest/
- https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-keys.html
- https://docs.aws.amazon.com/boto3/latest/reference/customizations/s3.html

