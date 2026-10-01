# 移民案件期限与材料管理

纯Python标准库实现的移民案件期限与材料管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 政策版本化（核心约定）

- 政策内容（各类案件的法定天数、补件期限、上诉窗口、必备材料）只存在于政策版本中，版本只增不改。
- **受理即快照**：创建案件时把当前政策版本号与期限参数写进案件（`policy_version`/`policy_snapshot`），之后新规不影响该快照。
- **草稿跟随新规**：管理员发布新版本后，所有草稿案件在同一事务内按新版本重算期限与材料清单（案件版本号随之+1，时间线记录`policy_recalc`事件）。
- **已提交/补件中钉住原版本**：`submitted`及之后状态的案件不被重算；补件期限（`evidence_allowed_days`）与上诉窗口（`appeal_window_days`）一律取案件快照。
- **回滚只生成新版本**：`POST /api/policies/rollback`复制目标旧版本内容追加为最新版本（记录`source_version`），历史版本永不修改。

## 并发、失败与权限

- 案件动作走乐观锁（`expected_version`）；政策发布/回滚走`expected_version`；受理走`expected_policy_version`。两方撞车时晚到的一方得到`409 conflict`，不会出现政策快照和材料各写一半。
- 所有多步写入（记录+审计+幂等键，或新政策+全部草稿重算）在单个`BEGIN IMMEDIATE`事务内完成，任何一步失败整体回滚，旧快照原样保留。
- 变更类接口支持`Idempotency-Key`请求头：响应与键在同一事务落库，网络重试/刷新重放返回首次结果，不会重复生效；同一键携带不同请求体会得到`409`。
- 案件按机构（`X-Org`）隔离：跨机构的读取、动作、时间线一律`403`，列表与统计只返回本机构数据；政策版本全局共享。

## 模块结构

- `app.py`：命令行参数、依赖组装（含初始政策种子）和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、政策校验、受理快照、草稿重算、补件期限和材料完整性检查。
- `src/repository.py`：SQLite建表（records/audit_events/policies/idempotency_keys）、事务和查询。
- `src/service.py`：用例编排、权限与机构检查、乐观并发、幂等重放和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、政策版本、并发冲突、失败恢复、幂等和机构隔离测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8329
```

默认端口为`8329`，默认数据库位于项目目录。服务启动时自动建表；`policies`为空时写入系统预置的v1政策。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/policies`：政策版本列表（新到旧）。
- `GET /api/policies/current`：当前政策版本。
- `POST /api/policies`：发布新版本，请求体`{"expected_version":1,"name":"...","rules":{"family":{"deadline_days":30,"evidence_allowed_days":10,"appeal_window_days":30,"required_documents":["passport"]},...}}`（rules须覆盖asylum/family/work三类，仅admin）。
- `POST /api/policies/rollback`：回滚，请求体`{"expected_version":2,"to_version":1,"name":"可选"}`，生成新版本（仅admin）。
- `GET /api/records`：本机构记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：本机构状态统计。
- `POST /api/records`：受理案件，请求体`{"reference":"...","expected_policy_version":1,"data":{"applicant_id":"...","case_type":"family","received_day":100,"response_day":110,"representation_active":true}}`；期限与必备材料取自当前政策快照。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体`{"expected_version":1,"data":{...}}`；`action`为`submit/request_evidence/respond/decide/appeal/close`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，机构隔离依赖`X-Org`（缺省为空机构）。所有POST接口接受可选的`Idempotency-Key`请求头。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、政策快照/重算/钉版/回滚、发布与受理撞车、双人提交冲突、写入失败回滚与幂等重试、跨机构拒绝。
