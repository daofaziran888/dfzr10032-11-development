# 多源数据采集与RPA自动化平台

面向网页抓取、API接入、增量采集、RPA流程编排、数据校验与采集监控的数据工程平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
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

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 数据源配置 | `datasource` | 数据源 | 源端编号、连接类型、接入地址 |
| 网页采集规则 | `crawler` | 采集规则 | 规则编号、目标网址、页面选择器 |
| API接入管理 | `apiclient` | API客户端 | 客户端编号、接口名称、请求方法 |
| 增量采集 | `incremental` | 增量游标 | 游标编号、来源标识、增量字段 |
| RPA任务编排 | `robot_task` | RPA任务 | 任务编号、任务名称、页面定位器 |
| 数据接入通道 | `ingestion` | 接入通道 | 通道编号、通道名称、输入格式 |
| 格式解析器 | `parser` | 解析配置 | 解析编号、输入格式、输出格式 |
| 采集结果校验 | `validator` | 校验规则 | 规则编号、校验字段、校验类型 |
| 采集去重 | `dedup` | 去重策略 | 策略编号、输入来源、去重键 |
| 采集调度 | `schedule` | 调度计划 | 计划编号、关联任务、调度周期 |
| 采集监控 | `monitor` | 监控面板 | 面板编号、采集源总数、今日采集量 |
| 采集日志 | `log_viewer` | 日志条目 | 日志编号、关联任务、日志级别 |
| 凭证管理 | `auth` | 认证凭证 | 凭证编号、凭证类型、关联数据源 |
| 采集转换管道 | `transform` | 转换节点 | 节点编号、输入来源、输出目标 |
| 数据流转编排 | `flow` | 流转管道 | 管道编号、管道名称、起始节点 |
| 数据血缘追踪 | `lineage` | 血缘关系 | 关系编号、上游节点、下游节点 |
| 采集报告 | `report` | 采集报告 | 报告编号、采集源、统计周期 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 采集去重归并与明细追溯

去重配置表（去重键、排序字段、时间窗口）之外，平台提供可追溯取舍的归并能力，
涉及三张数据集：输入事件 `dedup_event`、归并批次 `dedup_batch`、去重明细 `dedup_detail`。

- `POST /api/dedup/merge/events`：批量登记输入事件；事件时间落入已确认批次范围的
  标记为迟到事件，不拒收。
- `POST /api/dedup/merge/run`：按策略的去重键和有效时间范围批量归并待归并事件，
  整组的保留记录与被归并事件逐条写入去重明细数据集；归并使用的窗口与排序规则
  取自批次创建时的策略版本快照，与该批次记录的版本一致。
- `POST /api/dedup/merge/batches/{id}/confirm`：确认批次并冻结该版本；
  同一有效版本内一条事件不能既是保留记录又被归并（归并与确认时均校验）。
- 已确认版本冻结后，迟到事件只进入自动升代的新版本，每个已确认版本始终只指向
  一条明确的保留记录；`GET /api/dedup/merge/explain/{事件编号}` 解释迟到事件归到哪一版。
- `POST /api/dedup/merge/lineage`：数据血缘追踪根据去重明细数据集写出
  被归并事件 → 保留记录 的关系（保留记录自关联），每条输入事件都关联到
  实际生效的保留记录；重复同步自动跳过。
- `GET /api/dedup/merge/stats?策略编号=`：按版本对账，给出已确认版本（报告引用口径）
  与当前口径的保留记录差异。

验证：`cd backend && python3 tests/test_dedup_merge.py`（端到端覆盖版本快照、
互斥约束、迟到升代、血缘幂等与口径对账）。
