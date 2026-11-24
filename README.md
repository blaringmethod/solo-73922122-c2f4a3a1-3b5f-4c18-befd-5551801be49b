# 声级档案迁移核验 API

声级档案分批迁入新仓。档案员用本服务判断一卷旧档在新仓中是否已有足够的、
**尚未被其他清退回执占用**的副本：足够就签发“清退回执”并占用对应份数；
不足就逐行列出短缺。新仓按“片号”整片替换，且任何替换都必须保住全部已签回执
所需的份数。

- 技术栈：FastAPI + SQLite（标准库 `sqlite3`），Docker Compose 启动
- 交互文档：启动后打开 <http://localhost:8000/docs>（Swagger 静态资源已本地化，离线可用）
- 一键演示：`POST/GET /demo/two-volumes-contend`，或运行 `python scripts/demo.py`

---

## 1. 30 秒上手

### Docker Compose（交付方式）

```bash
docker compose up --build
# 打开 http://localhost:8000/docs
```

SQLite 数据库保存在 Compose 命名卷 `sound-level-archive-data` 内的
`/data/sound_archive.db`，容器重建不丢数据。

健康检查：`GET http://localhost:8000/health`。

### 本地直接运行（不用 Docker）

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt   # 仅运行服务可只装 requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 或：python main.py
```

数据库路径由环境变量 `SOUND_DB_PATH` 控制，默认 `./data/sound_archive.db`。

---

## 2. 在 /docs 演示“两卷旧档争用同一份读数”

争用整行：`M-DEMO,1760000000,612`（仪器 `M-DEMO`，UTC 秒 `1760000000`，
读数 `612` = 61.2 dB）。新仓里最初只有 1 份。

### 方式 A：一键（推荐先看这个）

在 `/docs` 找到 **演示 → `POST /demo/two-volumes-contend`**，点 **Try it out → Execute**；
也可以直接：

```bash
curl -X POST http://localhost:8000/demo/two-volumes-contend
# 或浏览器直接打开 GET http://localhost:8000/demo/two-volumes-contend
```

返回中 `all_checks_passed: true`，并带每一步的请求结果与 8 项断言。
每次运行使用随机后缀的片号、卷号和**仪器号（独立整行键）**，因此在同一个库上
反复调用也能稳定复现争用，不会被历史演示的全局占用份数干扰。

命令行端到端脚本（仅用 Python 标准库）：

```bash
python scripts/demo.py --base http://localhost:8000
```

### 方式 B：在 /docs 手工逐步点击（固定 ID，全新库）

> 想从头重来：`curl -X POST 'http://localhost:8000/admin/reset?confirm=true'`

1. `PUT /shards/SH-001`，请求体选 `text/csv`，粘贴
   ```
   M-DEMO,1760000000,612
   M-DEMO,1760000060,588
   ```
   （或用 `multipart/form-data` 的 `file` 上传 `examples/shard-SH-001.csv`，
   或在 `csv_text` 字段粘贴。）
2. `POST /volumes/VOL-A/submit`，body 填同样两行 → `outcome: "issued"`，记下 `receipt_id`。
3. `POST /volumes/VOL-B/submit`，body 只填 `M-DEMO,1760000000,612`
   → **409 `INSUFFICIENT_COPIES`**，
   `shortages[0]` 为 `required=1, total=1, occupied=1, available=0, short=1`。
4. 用第 2 步完全相同的内容再次 `POST /volumes/VOL-A/submit`
   → `outcome: "already_issued"`，返回**同一个 `receipt_id`**，不重复占用。
5. 把甲卷的 `612` 改成 `613`，仍提交到 `VOL-A`
   → **409 `VOLUME_CONTENT_MISMATCH`**。
6. `PUT /shards/SH-001` 换成 `examples/shard-SH-001-bad.csv`（不含争用行）
   → **409 `SHARD_COVERAGE_CONFLICT`**，`violations[0].receipts` 指出是哪张回执需要它；
   随后 `GET /shards/SH-001` 可见 `content_hash` 未变，旧片原样保留。
7.（可选）换成 `examples/shard-SH-001-good.csv`（争用行有 2 份）后替换成功，
   再 `POST /volumes/VOL-B/recheck`，乙卷即签发。

对应的 curl 版本：

```bash
B=http://localhost:8000
curl -X PUT $B/shards/SH-001 -H 'Content-Type: text/csv' \
  --data-binary @examples/shard-SH-001.csv
curl -X POST $B/volumes/VOL-A/submit -H 'Content-Type: text/csv' \
  --data-binary @examples/volume-A.csv
curl -i -X POST $B/volumes/VOL-B/submit -H 'Content-Type: text/csv' \
  --data-binary @examples/volume-B.csv
curl -X PUT $B/shards/SH-001 -H 'Content-Type: text/csv' \
  --data-binary @examples/shard-SH-001-bad.csv
```

---

## 3. CSV 与计数语义

- 三列：`仪器号, UTC整数秒, 十分之一分贝整数读数`。
  - 仪器号按去空白后的**原样字符串**处理，保留 `007` 这类前导零；
  - UTC 秒、读数必须是整数；读数 `612` 表示 61.2 dB（允许负整数）；
  - 三列**完全一致**才是“相同行”；相同行按**份**计（整行多重集）。
- 自动识别常见中英文表头（`instrument/utc/level...`、`仪器号/.../读数`），
  也可用 `?header=yes|no|auto` 显式指定；兼容 UTF-8 BOM、CRLF、字段周边空白、
  GB18030 回退解码。
- 内容身份 = 排序后的整行多重集的 SHA-256（见 `app/csvio.py`），
  因此**行顺序不同不算内容不同**，重复行份数不同则哈希不同。
- 畸形 CSV 返回 422 `CSV_PARSE_ERROR`，带逐行 `issues`（行号与原因）。

每种整行 key 的数量关系：

```
全仓总份数 total      = Σ 各分片 shard_rows.copies
已占用份数 occupied   = Σ 全部已签回执 receipt_rows.copies
未占用份数 available  = total - occupied
```

---

## 4. API 一览

完整 Schema 与在线试用见 `/openapi.json`、`/docs`。同一组接口也镜像在
`/api/...` 与 `/api/v1/...` 前缀下（不在 /docs 重复展示）。

| 方法 & 路径 | 作用 / 关键状态码 |
| --- | --- |
| `PUT/POST /shards/{shard_id}` | 整片创建/替换分片；破坏回执覆盖 → 409 回滚 |
| `POST /shards/{shard_id}/csv` | 同上的直观别名 |
| `POST /shards`、`PUT /shards` | 集合式，id 放 JSON/表单/`?shard_id=` |
| `GET /shards` / `GET /shards/{id}` / `GET /shards/{id}/csv` | 列表 / 详情含频次 / 原始 CSV |
| `DELETE /shards/{id}` | 删除（等价空片替换，仍受回执保护） |
| `POST /volumes/{volume_id}/submit` | 核验并签发；不足 → **409** + `shortages` |
| `POST /volumes/{volume_id}/verify` | 同 submit（足够即签发），但不足时返回 **200 + `sufficient:false`** |
| `POST /volumes/{id}/check` | 对已保存旧卷做**只读**核对，不签发、不写入 |
| `POST /volumes/{id}/recheck` | 用已存旧卷明细重新核验（补副本后重试，足够即签发，免重传） |
| `POST /volumes` | 集合式提交，id 放 JSON/表单/query |
| `GET /volumes` / `GET /volumes/{id}` | 列表 / 详情（含明细、回执） |
| `GET /volumes/{id}/receipt`（或 `/receipts`） | 按卷取原回执 |
| `GET /receipts` / `GET /receipts/{receipt_id}` | 回执列表 / 详情含逐行份数 |
| `POST /availability` | **只读**核对每行 total/occupied/available |
| `POST/GET /demo/two-volumes-contend` | 一键争用演示 |
| `GET /stats` | 分片/旧卷/回执/占用计数 |
| `GET /health`、`/healthz` | 健康检查 |
| `POST /admin/reset?confirm=true` | 清空全部数据（演示复位，生产请加鉴权） |

所有 CSV 接口都接受四种请求体：

1. `multipart/form-data` 的 `file`（/docs 文件框）或 `csv_text` 字段；
2. 原始 `text/csv` / `text/plain` body（curl `--data-binary @x.csv`）；
3. 被客户端默认标成 `application/x-www-form-urlencoded` 的原始 CSV（自动识别）；
4. `application/json`：`{"csv": "a,b,c\n..."}` 或
   `{"rows": [["M", 176..., 612], ...]}` / `{"rows": [{...别名键...}]}`。

错误体同时给出顶层 `code/message/shortages/violations`、FastAPI 风格的
`detail` 对象以及 `error` 对象，便于不同调用习惯读取。

---

## 5. 实现与一致性设计

### 目录结构

```
app/
  csvio.py      # CSV 解析、整行 Row、频次多重集、规范哈希、JSON 行转换
  db.py         # SQLite 连接、建表 SCHEMA、BEGIN IMMEDIATE 事务上下文
  errors.py     # AppError 体系与统一错误体
  services.py   # 核心业务：提交签发/占用、分片替换保护、查询、统计、重置
  demo.py       # 一键“两卷争用”用例（行键按运行隔离）
  main.py       # FastAPI 路由、输入协商、OpenAPI、异常处理、/docs
  static/       # 本地化 swagger-ui-bundle.js / css / favicon
scripts/demo.py # 纯标准库 HTTP 端到端演示与断言
examples/       # 手工演示用 CSV（初始片、甲卷、乙卷、好/坏替换片）
tests/          # 46 个测试：CSV、API 全流程、并发争用、持久化、大卷分批与快照一致性
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```

### SQLite 表（`app/db.py`）

- `shards` / `shard_rows`：分片元数据（原文、哈希、行数）与片内整行频次；
- `volumes` / `volume_rows`：旧卷内容哈希、状态（`short|issued`）、绑定的回执；
- `receipts` / `receipt_rows`：清退回执及逐行份数（**占用的事实记录**）；
- `occupancy`：每种整行的累计占用份数，由签发事务增量维护
  （可随时由 `receipt_rows` 重建核对）。

整行三列建复合主键，并有 `(instrument, ts, level, copies)` 索引支撑按行键汇总。

### 关键不变量与事务

任何已提交状态都满足：

```
对每个整行 key：Σ shard_rows.copies >= Σ receipt_rows.copies（= occupancy.occupied）
```

- **提交旧卷**（`services.submit_volume`）在单个 `BEGIN IMMEDIATE` 事务内：
  读旧卷绑定 → （已签发则直接返回原回执）→ 按本次行键 CTE 汇总 total/occupied →
  判定短缺或写 `receipts/receipt_rows`、增量更新 `occupancy`、更新 `volumes`。
  任一步异常都 `ROLLBACK`，回执/份数/卷状态保持原状。
- **同卷幂等**：卷号首次出现即绑定内容哈希；同哈希重送返回同一回执；
  不同哈希抛 `VOLUME_CONTENT_MISMATCH`（事务内尚未写入，直接回滚）。
  首次不足也会登记旧卷明细，之后可用 `/recheck` 重试；不同内容同样拒绝。
- **分片整片替换**（`replace_shard`）在一个 `BEGIN IMMEDIATE` 事务内：
  只取旧片频次 + 新旧并集行键的全仓汇总 + 这些行的回执分配；
  若 `current_total - old_copies + new_copies < occupied`，收集 `violations`
  （含需要该行的回执列表）后抛 `SHARD_COVERAGE_CONFLICT` 回滚，旧片不动；
  否则“删旧频次 + 写新频次 + 更新元数据”一次提交。跨片副本按可互换的份处理：
  只要全仓总数仍覆盖全部回执即可（有专门测试）。
- **并发争用**：SQLite 写事务全局串行 + `busy_timeout=15s`，每请求独立连接、
  WAL、外键开启。两个旧卷同时抢一份时，只有一个事务能看到 `available>=need`
  并提交，另一个等待后读到新的已提交占用，判短。即使 Compose 扩成多个
  uvicorn 进程，`BEGIN IMMEDIATE` 仍由数据库级锁保证正确。
- **只读核对的单快照语义**：`/check`、`/availability` 以及分片/卷/回执详情等
  需要多条 SELECT 的只读路径，都包在一个延迟 `BEGIN` 读事务里。事务在第一条
  语句时固定快照，因此卷状态、回执号与逐行 total/occupied/available 一定来自
  **同一个已提交状态**——即使核对进行中另一连接提交了补签，也不会出现
  “响应显示未签、回执号为空，余量却已被扣减”的跨状态拼接。WAL 下读事务不
  阻塞写事务，另一个连接的补签照常提交，只是本次核对看到的是开始时的快照。
- **大卷分批，参数不超限**：按整行键的可用性 CTE 每个键占 3 个绑定参数，
  旧版 SQLite 单条 SQL 上限只有 999、新版默认 250000。`fetch_availability`
  读取连接的实际上限（`SQLITE_LIMIT_VARIABLE_NUMBER`）自动拆批，键先去重，
  每批 `3*N + 预留余量` 保证不超限；所有批次在同一事务（写路径的
  `BEGIN IMMEDIATE` 或只读路径的快照事务）内执行，读到同一快照、拼起来仍是
  一致状态。整卷提交、`/verify`、`/recheck`、`/availability`、整片替换与删除
  全部走这条分批路径，超过单条 SQL 参数容量也能把本次涉及的整行**全部查全**，
  不再返回 500。
- **只读涉及数据**：核对/替换都用 `WITH k ... VALUES ...` 仅查本次行键，
  不扫全表；只有 `GET /shards`、`/stats` 等列表/统计接口做聚合。

---

## 6. 实测结果（交付前真实试跑）

试跑环境：沙箱容器，Python 3.11.2，SQLite 3.40.1；依赖
fastapi 0.165.0、uvicorn 0.54.0、python-multipart 0.0.34、httpx 0.28.1、pytest 9.2.0。

### 6.1 自动化测试：46/46 通过

```
46 passed, 1 warning in ~48s
```

完整逐条结果见 [`docs/test-results/pytest.txt`](docs/test-results/pytest.txt)。覆盖：

- CSV：有无表头、中英文表头、BOM/CRLF、前导零、重复行计份、行序无关哈希、
  读数小数/列数错误按行报错、JSON 行转换（15 个）；
- API 全流程：健康/文档/前缀镜像、签发与幂等重送、换序+表头仍判同内容、
  内容篡改 409、两卷争用一份的短缺字段、多份数、短缺卷绑定、只读 `/check` 与
  recheck、坏替换 409 且旧片不变、补足后等待卷签发、跨片总量足够可替换、
  删除保护、畸形输入不留痕、四种输入形式、urlencoded 原始 CSV 回归、
  只读 availability、reset 确认、一键演示、**重启后 SQLite 持久化**（21 个）；
- 并发（真实文件库 + 8/10 线程 + Barrier）：
  - 1 份热点副本、8 卷并发 → **恰好 1 卷 `issued`、7 卷 `insufficient`**，
    `receipts=1, occupied_copies=1`；
  - 3 份、10 卷并发 → **恰好 3 卷成功**，占用 3；
  - 同一卷 8 线程重送 → **全部返回同一 receipt_id**，回执仍 1 张、占用不翻倍；
- 本次修复回归（`tests/test_fixes.py`，7 个）：
  - **90000 个不同整行**（单条 SQL 需 270000 个参数 > 上限 250000）：
    分片替换、`/availability`、整卷提交、recheck 全部成功且**90000 行查全**；
  - 临时把连接变量上限压到 30，验证分批器本身按每批 4 键拆分、合并不漏行；
  - 大卷每行要 2 份、仓里只有 1 份 → 逐行列出 1000 种短缺、**不签回执、零占用**，
    补够后 recheck 签发并占用 2000 份；
  - 大键集换片会使已签回执缺份 → 409 逐行列出违例、**旧片保留**；
  - 只读 `/check` 进行中另一连接提交补签 → 响应的卷状态/回执号/余量要么全是
    补签前快照、要么全是补签后快照，**不允许跨状态拼接**；补签提交后的新连接
    完整看到已签状态；
  - HTTP 级大卷不再 500、争用 409 与同卷幂等重送语义保持不变。

### 6.2 真实 uvicorn + HTTP 端到端：10/10 断言通过

命令：

```bash
SOUND_DB_PATH=/tmp/run/archive.db \
  python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
python scripts/demo.py
```

结果（完整输出 [`docs/test-results/demo-http.txt`](docs/test-results/demo-http.txt)）：

```
[PASS] 分片创建 200
[PASS] 甲卷签发 200
[PASS] 乙卷短缺 409 且 total=1/occupied=1/available=0/short=1
[PASS] 重送返回原回执
[PASS] 篡改内容 409
[PASS] 坏替换 409
[PASS] 拒绝后旧片不变
[PASS] 好替换 200
[PASS] 乙卷补足后签发
[PASS] 两张不同回执；本次争用行 total=2 且 occupied=2
10/10 checks passed
```

一键端点调用 `all_checks_passed: true`
（摘要 [`docs/test-results/demo-one-click-summary.json`](docs/test-results/demo-one-click-summary.json)，
完整响应同目录 `demo-one-click-full.json`）。

### 6.3 大卷真实 HTTP 核验：13/13 断言通过（在 /docs 同栈试跑）

用真实 uvicorn 进程、经 HTTP 跑 90000 个不同整行（若写成单条 SQL 需 270000
个绑定参数，超过本机 SQLite 250000 的上限），完整结果见
[`docs/test-results/large-volume-http.json`](docs/test-results/large-volume-http.json)：

```
[PASS] admin reset
[PASS] 大卷分片 PUT 200（90000 键，270000 参数）
[PASS] 大卷 /availability 查全 90000 行且 sufficient
[PASS] 大卷缺份 submit 409 且列出全部短缺
[PASS] 短缺后无回执、零占用
[PASS] 补副本换片 200
[PASS] 大卷 recheck 签发，占用 180000 份
[PASS] 破坏性换片 409，逐行列出违例
[PASS] 拒绝后旧片保留（180000 物理行）
[PASS] 两卷争用最后一份：A 签、B 短缺 409
[PASS] 同卷原样重送取原回执
[PASS] 终态：2 张回执，占用 180001 份
[PASS] 大卷回执详情含全部 90000 行、每行 2 份
13/13 checks passed
```

修复前同样的大卷请求在分片替换和可用性核对处直接抛
`sqlite3.OperationalError: too many SQL variables`，经全局异常处理变成 HTTP 500；
修复后由分批器自动拆成多条查询、在同一事务快照内合并，行数查全且状态一致。

### 6.4 Docker 验证范围（如实说明）

本沙箱内**没有 docker CLI，也没有 `/var/run/docker.sock`/守护进程**
（当前本身就在容器中），因此无法在这里真正执行 `docker compose up --build`。
已完成的相关核对：

- `docker-compose.yml` 通过 PyYAML 解析，service/volume/healthcheck 结构与命令合法；
- `Dockerfile` 使用 `python:3.11-slim`、固定 `requirements.txt`、非 root 用户、
  `/data` 持久卷与 HEALTHCHECK，启动命令就是已实测的
  `uvicorn app.main:app --host 0.0.0.0 --port 8000`；
- 服务本身已用同一启动命令在真实 uvicorn 上完成上述全部关键操作试跑（含大卷）。

所以**首次在交付环境执行 `docker compose up --build` 仍建议盯一下构建/健康检查**，
这是下方待办的第一条。

---

## 7. 待办 / 可增强项

1. **在有 Docker 的环境实跑一次** `docker compose up --build`，确认镜像拉取、
   pip 安装、命名卷权限与 HEALTHCHECK（沙箱无守护进程，未能代跑）。
2. 生产化安全：当前无认证/授权，`/admin/reset` 仅靠 `confirm=true` 防误触；
   若要暴露到内网/公网，应加 API Token / mTLS，并把 reset 改为管理员鉴权或下线。
3. 大文件与配额：目前未限制 CSV 体积/行数，也没有分页；建议加 body 大小上限、
   流式解析、`GET` 列表分页。
4. 可观测性：`/stats` 之外可补结构化访问日志、Prometheus 指标、审计日志
   （谁在何时签发/替换/拒绝）。
5. 备份与扩展：SQLite 适合单机与 Compose；建议对命名卷做快照或 Litestream 备份。
   若将来要多实例水平扩展，可把 `app/db.py` 的 SQL 平滑迁到 Postgres
   （事务与 `BEGIN IMMEDIATE` 语义需换成行级锁/`SELECT ... FOR UPDATE` 或唯一约束）。
6. 回执可增加 PDF/JSON 导出、作废/换发流程；目前“回执只增、占用只增”，
   尚无退回收回副本的业务端点（题目未要求）。
7. 可补充更多 CSV 方言（分号分隔、表头别名）与仪器号归一化规则——目前刻意保持
   仪器号原样、三列逗号分隔，以免把 `007`/`7` 误并为同一行。
