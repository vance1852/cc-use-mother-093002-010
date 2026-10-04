# 固化国际数字规则共识协作基础服务

本项目提供跨境数字贸易合作业务共享的服务端基础能力，负责合作机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域模块可以在这些稳定边界上扩展自己的状态、规则和接口。

## 规则文本协商与承诺跟踪

`negotiation.NegotiationService` 在基础能力之上为国际对话机制提供可追溯的规则文本协商流程，覆盖提案与条款版本、翻译对应、发言授权、利益冲突、保留意见、生效条件、签署资格与后续行动：

- 立场（接受 `accept`、条件接受 `conditional_accept`、保留 `reserve`、反对 `reject`）始终针对同一文本快照（条款版本），支持情况按快照计算；同一代表团重复表态只更新记录，不增加支持数；
- 修订须基于当前头版本并获得其他代表团附议才能合并，合并产生新版本，同基础的其余修订自动标记为冲突，互相冲突的修订不能同时合并；
- 翻译与条款版本一一对应，须由非提交人的翻译审校核定，同一版本同一语言只能有一份审定译文；
- 发言须持有秘书处签发且在有效期内的授权（可限定条款范围）；场次封存后，封存范围内的发言与表决事实不再受后续文字整理影响，同一场次的并发封存只能有一个结果，封存清单可离线校验；
- 达到程序要求（法定人数、零反对、支持数达到阈值）后只形成待核准共识；承诺须等国内或组织前置条件按顺序满足后才对对应参与方生效，其他独立条款不被一项保留意见拖住；
- 所有状态持久化在 SQLite 中，服务恢复后继续等待条件和行动期限，逾期状态按当前时间即时计算；
- 公众接口 `GET /dialogues/{id}/public` 清楚区分草案（`draft`）、已接受（`accepted`）、保留（`reserved`）与已生效（`in_effect`）；代表、翻译审校、秘书处和观察员通过 `GET /dialogues/{id}/view` 获得不同视图；
- 审计查询 `GET /clauses/{id}/binding?delegation_id=&at=` 解释某个文本在指定时点为何对特定参与方具有或不具有约束。

新增操作者角色：`secretariat`（秘书处）、`delegate`（代表）、`translator`（翻译审校）、`observer`（观察员）。主要接口：

- POST /dialogues、/dialogues/{id}/delegations、/dialogues/{id}/grants、/dialogues/{id}/statements、/dialogues/{id}/proposals、/dialogues/{id}/seals、/dialogues/{id}/actions、/dialogues/{id}/coi
- POST /proposals/{id}/clauses、/clauses/{id}/amendments、/amendments/{id}/seconds|withdraw|merge
- POST /versions/{id}/translations、/versions/{id}/positions、/versions/{id}/consensus、/translations/{id}/verify
- POST /commitments/{id}/conditions/{seq}/fulfill、/actions/{id}/complete、/coi/{id}/clear、/grants/{id}/revoke、/statements/{id}/revisions
- GET /versions/{id}/tally、/dialogues/{id}/public、/dialogues/{id}/view、/dialogues/{id}/actions、/dialogues/{id}/commitments、/commitments/{id}、/clauses/{id}、/clauses/{id}/binding、/seals/{id}

## 目录

- src/digital_trade_foundation/：领域模型、SQLite 存储、权限服务、审计链、协商与承诺跟踪领域服务、HTTP 路由和离线验收；
- tests/：基础规则、事务边界、接口路由、协商流程和端到端验收测试。

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

验收命令会在临时 SQLite 数据库中登记合作机构、操作者、业务节点和参考资料，核对幂等回执与审计链，成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m digital_trade_foundation.api --database digital_trade.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。
