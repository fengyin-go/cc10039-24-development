# 数据湖分区与存储治理中心

管理数据湖分区策略、存储优化、冷热分层和小文件合并的数据湖治理平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8014/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5194/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8014`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 分区策略 | `partition_policy` | 分区策略 | 策略名称、分区维度、分区粒度 |
| 湖表注册 | `lake_table` | 湖表 | 表名、所属业务域、存储格式 |
| 冷热分层 | `storage_tier` | 分层策略 | 策略名称、热层阈值、冷层阈值 |
| 小文件合并 | `file_compaction` | 合并任务 | 任务编号、目标湖表、文件阈值 |
| 数据目录 | `data_catalog` | 目录条目 | 条目名称、数据层级、所属湖表 |
| 湖内质量监控 | `quality_monitor` | 监控规则 | 规则名称、监控指标、告警阈值 |
| 保留策略 | `retention_mgr` | 保留策略 | 策略名称、适用湖表、保留天数 |
| 入湖管道 | `ingest_pipeline` | 入湖任务 | 任务名称、来源系统、目标湖表 |
| 快照版本 | `snapshot_version` | 快照版本 | 湖表名、快照时间戳、版本号 |
| 访问审计 | `access_audit` | 访问记录 | 访问者、目标湖表、操作类型 |
| Schema演进 | `schema_evolution` | Schema变更 | 湖表名、变更类型、变更字段 |
| 负载均衡配置 | `load_balance` | 分片配置 | 湖表名、当前分片数、分片策略 |
| 元数据同步 | `metadata_sync` | 同步任务 | 任务名称、源端类型、目标端类型 |
| 数据回填任务 | `backfill_job` | 回填任务 | 任务名称、目标湖表、回填时间范围 |
| 数据字典管理 | `data_dictionary` | 字典条目 | 字典名称、适用范围、值域列表 |
| 任务调度 | `task_scheduler` | 调度任务 | 任务名称、调度表达式、关联管道 |
| 存储告警规则 | `alert_rule` | 告警规则 | 规则名称、告警指标、告警级别 |
| 存储成本分析 | `cost_report` | 成本报告 | 报告周期、目标业务域、存储总量 |
| 湖表血缘 | `lineage_graph` | 血缘关系 | 上游源、中间转换、下游消费 |
| 治理报告 | `govern_report` | 治理报告 | 报告周期、治理指标、优化建议 |

### 字段补数缺口（Schema 演进 ↔ 数据回填联动）

回填检查点只能给出已处理条数，无法回答“某一列在哪些日期还缺数据”。
`Schema演进` 每次对同列执行不兼容变更都会分配递增字段版本（v1、v2……），
版本携带允许写入的列值和补数日期范围，执行变更时由回填服务逐日登记
**字段补数缺口**（内存表 `backfill_gap`），并在补数写入后生成
**补数记录**（内存表 `backfill_fill`）。

- 缺口查询：按湖表、字段版本（元数据版本ID）、日期范围、缺口状态筛选待补部分；
  每条缺口都带元数据版本ID和回填任务ID，可双向追溯。
- 回填核算：Schema 演进用缺口查询结果核算下一次字段变更的回填输入，
  返回待补天数、合并后的可执行日期范围与该版本允许写入的列值；
  实际启动任务时只使用查询选中的日期范围和允许列值。
- 幂等补数：同版本同日期重复提交已补完的日期会被拦截，只保留第一次完成的补数记录。
- 同列两版变更：同一天可同时存在两个版本的缺口；新版本允许列值收窄后，
  旧值写入会留痕并把缺口置为「待复核」，重叠日期复核页可查看每天各版本
  写入了哪些字段数据、哪些值仍待复核。

专用端点（路由见 `backend/app/routers/backfill.py`）：

| 端点 | 用途 |
| --- | --- |
| `POST /api/backfill/field-changes` | 登记带补数范围与允许列值的不兼容字段变更草稿 |
| `GET  /api/backfill/field-versions` | 查询同表同列的字段版本列表 |
| `POST /api/backfill/gaps/query` | 字段补数缺口查询（湖表/版本/日期范围筛选） |
| `POST /api/backfill/gaps/plan` | 核算下次字段变更关联的回填输入 |
| `POST /api/backfill/gaps/start` | 按选中日期范围与允许列值启动回填任务 |
| `POST /api/backfill/gaps/fill` | 提交单日补数（幂等，越界值转待复核） |
| `POST /api/backfill/gaps/overlap-review` | 同列多版重叠日期写入/复核检查 |
| `GET  /api/backfill/fills` | 补数记录查询（追溯版本与任务） |

前端在「数据回填任务」之后新增 **字段补数缺口** 页面（`/backfill_gap`，
`frontend/src/views/BackfillGapView.vue`），含缺口查询与补数、重叠日期复核、
补数记录三个页签。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数仓与数据湖
