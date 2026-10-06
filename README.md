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
- `window`：装置停车窗口（容量按重叠时段计数，半开区间 `[start, end)`）。
- `booking`：变更对窗口一段时段的占位；不单独建实体，经窗口接口生成，状态为
  `scheduled / queued / consumed / released`，并在 `schedule_ledger` 中按
  `seq`（申请顺序）记账。

## 停车排程账规则

- 窗口容量按“与占位记录时段重叠”的条数计算；容量满时新申请登记为 `queued`，不占位。
- 两名工程师同时抢同一空窗口：数据库写锁内先到者 `scheduled`，晚到者收 409；
  顺序提交遇满员则是 202 `queued`，二者区分。
- `POST /api/windows/<id>/recover` 按申请顺序重试排队项，队首个放不下即停止；
  带同一 `Idempotency-Key` 的重试返回原记录，不重复登记、不占位。
- 占位在变更 `implement` 时转为 `consumed`，`commission/rollback` 离开窗口后释放，
  释放自动按 FIFO 提升队首；也可 `POST /api/bookings/<id>/release` 手工释放。

## 实施门禁与风险联动

- `implement` 前置：变更必须已有 `scheduled` 占位，且所有行动项 `verified`
  （`cancelled` 为作废措施，不阻塞）。
- 已 `implemented` 的变更下任一行动项 `reopen`：变更自动退回 `approved` 并记录
  `evaluation_reset`，需重新评估、重新复核后才能再次实施。
- 已 `commissioned`（投产）的变更重开行动项只追加事实，不改原状态、原风险结论。
- `reassess` 改变风险等级：未复核（open/completed）行动项的结论作废回 `open`；
  已 `verified` 的结论保留；按新等级重算必备措施（缺项补开、不再要求且未复核的
  作废），状态回到 `assessed` 重新审批。对 `commissioned` 变更仅追加
  `risk_reviews`，原记录不动。

## 旧数据升级

- SQLite `PRAGMA user_version` 升到 2 时自动回填：行动项补 `review`（按是否
  verified），变更补 `implementation_state`（投产类→fulfilled、已批准且项全部
  复核→ready、否则 pending）和按旧风险等级推导的 `required_measures`；
  每次回填写一条 `migration_backfill` 审计，可重复打开且不重复回填。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用 `?status=`过滤（含 `window`/`booking`）。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `POST /api/windows/<id>/bookings`：提交占位/排队，`Idempotency-Key` 标识申请；
  返回 201 scheduled / 202 queued / 200 同键重试 / 409 并发抢锁失败。
- `POST /api/windows/<id>/recover`：按申请顺序恢复排队。
- `POST /api/bookings/<id>/release`：释放占位并提升队首。
- `GET /api/windows/<id>/schedule`：读取窗口排程账（按申请顺序）。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

变更动作：`assess → approve → implement → commission`，`reassess` 可在
`assessed/approved/commissioned` 上使用；行动项：`complete → verify`，`reopen`
触发上述级联。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

风险分级和投产规则用于流程演示，不替代HAZOP、LOPA、法定许可和现场安全审查。
