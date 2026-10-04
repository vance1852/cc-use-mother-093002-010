# 固化国际数字规则共识协作基础服务

本项目提供跨境数字贸易合作业务共享的服务端基础能力，负责合作机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域模块可以在这些稳定边界上扩展自己的状态、规则和接口。

`digital_trade_foundation.negotiation` 在基础服务之上实现**规则文本协商与承诺跟踪**：把提案与条款版本、翻译对应、发言授权、利益冲突、保留意见、生效条件、签署资格和后续行动连成一条可追溯的过程链。

## 目录

- src/digital_trade_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由、协商服务和离线验收；
- tests/：基础规则、协商规则、事务边界、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m digital_trade_foundation.acceptance
    PYTHONPATH=src python3 -m digital_trade_foundation.acceptance_negotiation

验收命令会在临时 SQLite 数据库中完成登记/协商全链路并输出一行 status 为 ok 的 JSON。协商验收覆盖：同一文本快照计票、互相冲突修订互斥、封存事实与后续文字整理隔离、重复签署不计数、前置条件按序生效、保留意见只拖住本方、服务恢复后继续等待期限、公众接口状态区分与时间点约束力解释。

## HTTP 服务

    PYTHONPATH=src python3 -m digital_trade_foundation.api --database digital_trade.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留；服务启动时会先按当前时钟对账条件与行动期限。

## 协商领域规则

- **提案与条款**：`POST /proposals`、`POST /clauses`。每次条款文本变化生成不可变 `text_versions` 快照（初始、修订、译本、文字整理）。
- **修订与同一快照计票**：`POST /amendments` 提出修订并固定 `base_version_id`；`POST /amendments/stance` 附议/反对/条件接受/撤回，支持数只统计基于同一基础快照、代表团启用、无未清利益冲突且非提案方的代表团。`POST /amendments/merge`（秘书处）达到附议门槛后才能合并；同一基础快照至多一个修订合并，竞争修订自动标记 `conflicted`。
- **封存**：`POST /seals` 封存某修订/条款范围内的表决事实、发言与签署，聚合哈希落库；`GET /seals/verify` 重算聚合并回源比对。并发封存同范围只有一个结果，封存之后的译本和文字整理不改变封存事实。
- **立场、签署与待核准共识**：`POST /clauses/stance` 后，只有对当前快照作接受/条件接受的代表才能 `POST /clauses/sign`；重复签署幂等且不增加支持数。支持数达到提案门槛只形成 `pending_ratification` 共识，条款进入"已接受（待核准）"。
- **前置条件与生效**：`POST /conditions` 登记国内批准/组织核准/通知/交存等条件（有顺序和期限），`POST /conditions/satisfy` 必须按序满足，逾期经 `POST /sweep-deadlines` 判定失效且不可补记。某参与方签署有效、共识已形成、其自身前置条件全部按序满足且无有效保留时，承诺才对该参与方生效；其他独立条款与其他参与方不受影响。
- **保留意见**：`POST /reservations` / `POST /reservations/withdraw`，只阻止本方承诺生效，撤回后自动复核生效。
- **翻译对应**：`POST /translations`（翻译）、`POST /translations/certify`（审校）建立快照间语言对应。
- **角色视图**：`GET /views/delegate`（本代表团授权/立场/条件/行动）、`GET /views/translator`（译本对应）、`GET /views/secretary`（门槛/共识/逾期）、`GET /views/observer`（封存与发言事实），按角色隔离。
- **公众接口**：`GET /public/clauses` 对每条款明确区分草案（`draft`）、已接受待核准（`accepted`）、已生效（`in_force`），并列出有效保留与逐方生效时间。
- **时间点审计解释**：`GET /audit/binding?clause_id=...&delegation_id=...&at=...` 沿"初始快照+已合并修订链"还原指定时点的有效文本，解释立场、签署、共识、前置条件与保留如何导致具有/不具有约束力。
