# 化工装置变更与工艺安全管理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8310`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8310
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `unit`：装置运行状态；`change`：变更申请；`action_item`：风险控制行动项。
- `window`：装置停车窗口（含容量）；`schedule`：变更对窗口的占用记录，状态为 `scheduled` 或 `queued`。

## 停车排程账

变更批准后可通过 `schedule` 动作排入装置窗口：`POST /api/entities/<change_id>/actions`，请求体 `{"action":"schedule","data":{"window_id":"..."}}`。

- 每张变更占用窗口一个容量；窗口容量满时新申请进入 `queued` 排队，排程账可通过 `GET /api/schedules` 查看。
- 排程写入失败后，`POST /api/recover` 按申请顺序恢复排队记录；已占位的排程不重复占位（重试幂等）。
- 同一变更重复排程返回冲突；携带 `idempotency_key` 的重试不占位。
- 变更实施（`implement`）前，其下所有行动项必须复核通过；任一行动项 `reopen` 后，已开始的实施退回 `approved` 并标记重新评估。
- 风险等级变更时，未复核的行动项作废并重算措施；已投产（`commissioned`）的变更保留原记录。
- 升级时旧数据缺少落实状态，`POST /api/backfill` 按已有复核记录回填 `implementation_status`，历史变更仍可正常读取。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

风险分级和投产规则用于流程演示，不替代HAZOP、LOPA、法定许可和现场安全审查。
