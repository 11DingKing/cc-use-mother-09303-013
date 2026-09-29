# 国际论坛项目撮合

论坛结束后，同一家企业可能被多所院校分别承诺使用同一批实训资源，联系人收到互相冲突的跟进邮件，原始合作意向也无法对齐。本项目在领域契约之上提供一套完整后端，统一保存机构能力、需求版本、排除条件、资源容量与授权联系人，生成带理由的候选关系并由双方独立确认；在撤回意向、联系人失效、需求变更、资源被占用时自动重新评估尚未签约的候选；转正式项目后冻结匹配依据，并支持从最初意向一直追踪到签约或终止原因。

## 领域模型与状态机

- **机构与授权联系人**：职业院校 / 合作企业；联系人由秘书处授权，失效后不能再代表机构确认，也不再收到跟进通知。
- **资源批次与容量**：自有资源与秘书处登记的共享实训资源统一管理，容量被正式项目预留后，竞争同一批次的未签约候选自动失效。
- **机构能力 / 排除条件**：能力声明参与评分；排除条件（机构级或全局）命中时拒绝撮合，新增排除立即重评估在途候选。
- **意向与不可变需求版本**：每次需求变更追加新版本而不覆盖旧版本，候选始终引用具体版本号。
- **候选关系**：`proposed → confirmed → contracted`，支路 `superceded / invalidated / rejected / terminated`；双方授权联系人必须各自独立确认。
- **正式项目**：创建时冻结需求版本、评分理由、确认人与容量快照，此后任何变更不影响签约依据；项目只能终止并记录原因、释放容量。
- **谱系事件**：登记、撮合、确认、失效、接替、转项目、终止全程留痕，可按意向 / 候选 / 项目任一入口回放。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/matchmaking/`：撮合后端
  - `models.py` 领域模型与状态枚举；`engine.py` 确定性撮合与重评估；
  - `service.py` 用例编排（登记/撮合/确认/重评估/转项目/追踪）；
  - `repository.py` 内存仓储（可替换为数据库实现）；`api.py` 标准库 HTTP 接口。
- `tools/check_contract.py`：契约命令行摘要。
- `tools/demo_scenario.py`：端到端业务场景演示（一企业 × 三院校 × 同一批共享资源）。
- `tests/`：契约与撮合后端回归测试。

## 快速开始

```bash
# 端到端场景演示（无需任何第三方依赖）
python3 tools/demo_scenario.py

# 启动 HTTP 服务（默认 8080）
PYTHONPATH=src python3 -m matchmaking.api 8080
```

主要 HTTP 端点：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/orgs`、`/orgs/{id}/contacts|capabilities|resources|exclusions|intents` | 登记机构、授权联系人、能力、资源、排除、意向 |
| POST | `/contacts/{id}/revoke` | 联系人失效，重评估相关未签约候选 |
| POST | `/resources/{id}/occupy` | 资源被外部占用，按剩余容量重评估 |
| POST | `/intents/{id}/revise`、`/withdraw` | 需求改版（旧候选 SUPERSEDED 并重撮合）/ 撤回意向 |
| POST | `/matching/run` | 生成带评分与理由的候选 |
| POST | `/candidates/{id}/confirm` | 一方独立确认或拒绝 |
| POST | `/candidates/{id}/project` | 双方确认后转正式项目并冻结依据 |
| POST | `/projects/{id}/terminate` | 终止项目、记录原因、释放容量 |
| GET | `/intents/{id}/timeline`、`/lineage`、`/snapshot` | 全链路追踪与总览 |

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
```
