# 移民案件期限与材料管理

纯Python标准库实现的移民案件期限与材料管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

核心约束：**政策频繁调整，但案件的期限不随新规漂移**。受理时把当时的政策整体快照进案件；
管理员发布新版本后，只有草稿案件按新版本重算，已经提交或处于补件中的案件继续认原版本，
补件期限也从案件自己的政策快照推导；政策回滚不改正史，只追加一个新版本。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：政策内容校验、受理快照、草稿重算、状态转换、法定/补件期限。
- `src/repository.py`：SQLite建表、政策版本表、事务与两类乐观锁。
- `src/service.py`：用例编排、机构隔离、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：规则计算、完整流程、失败场景与政策版本化测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8329
```

默认端口为`8329`，默认数据库位于项目目录。服务首次启动自动建表并写入种子政策（版本1）。

## 政策与版本模型

政策内容按案件类型配置，结构为：

```json
{
  "deadline_days": {"asylum": 60, "family": 30, "work": 20},
  "evidence_days": {"asylum": 30, "family": 10, "work": 10},
  "required_documents": {"asylum": ["passport", "asylum_statement"], "...": []},
  "appeal_days": 30
}
```

- **受理快照**：案件行存`policy_version`，payload内嵌完整`policy`快照与由快照推导出的
  `deadline_day`/`required_documents`，与材料在同一事务写入。
- **发布新版本**：只追加版本行并推进当前版本指针；同一事务内把所有`draft`案件重算
  （快照、期限、材料清单整体切换，审计记录`policy_rebased`）。
- **提交后冻结**：`submitted`/`evidence_requested`/`response_received`等状态的案件永不重算；
  补件窗口天数取案件快照的`evidence_days`，请求方不能自行指定。
- **回滚**：复制任一历史版本的内容发布为新版本（标记`rollback`与`source_version`），
  历史版本行永不修改。

## 并发与失败语义

- 两位书记员同时提交同一案件：案件行版本号乐观锁，晚到一方收到`409 conflict`，
  刷新后带新版本重试；成功写入只有一次。
- 发布与受理撞车：受理在写锁内核对全局政策版本指针，指针已变则整体回滚并返回`409`，
  提示按新版本重新受理，不会留下“旧快照+新材料清单”的半成品。
- 发布同样携带`expected_version`，两次发布基于同一旧版本时，后到者`409`。
- 写入失败（含提交阶段失败）：事务回滚，旧快照、旧版本、审计均保持原样，重试不重复生效。
- 机构隔离：案件记录归属受理机构（`X-Org`），查看、动作、时间线、列表一律强制同机构，
  跨机构操作返回`403`。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/policies/current`：当前政策版本。
- `GET /api/policies`：全部历史版本。
- `GET /api/policies/{version}`：指定版本内容。
- `POST /api/policies`：管理员发布新版本，请求体`{"expected_version":1,"content":{...}}`。
- `POST /api/policies/rollback/{version}`：回滚到历史版本（只追加新版本），
  可带`{"expected_version":2}`做乐观检查。
- `GET /api/records`：本机构记录列表，可带`state`和`limit`。
- `GET /api/records/{id}`：记录详情（含政策快照）。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：受理案件，请求体
  `{"reference":"...","data":{"applicant_id":"A-1","case_type":"family",
  "received_day":100,"response_day":110,"representation_active":true},
  "expected_policy_version":1}`，其中`expected_policy_version`省略时按当前版本受理。
  法定期限与必备材料以当前政策为准，请求无需提供。
- `POST /api/records/{id}/actions/{action}`：执行业务动作
  （`submit`/`request_evidence`/`respond`/`decide`/`appeal`/`close`），
  请求体`{"expected_version":1,"data":{...}}`；`request_evidence`的补件天数由政策快照决定。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`、`X-Org`头。
仅`admin`角色可发布/回滚政策。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖政策快照冻结、发布只重算草稿、回滚只追加、双书记员并发、受理/发布撞车、
写入失败原子性与重试不重复、跨机构拒绝，以及完整流程与规则计算。
