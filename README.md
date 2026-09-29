# 国际论坛项目撮合

本项目维护国际论坛项目撮合的领域约定、角色边界与样例数据，并提供一套**零外部依赖**的完整后端：
保存机构能力、需求版本、排除条件、资源容量与授权联系人，生成**带理由**的候选关系并由
企业与院校**独立确认**；在意向撤回、联系人失效、需求变更或资源被占用时自动重评估尚未签约的候选；
转为正式项目后**冻结匹配依据**；使用者可从最初意向一路追踪到签约或终止原因。

## 要解决的问题

论坛结束后，同一家企业可能被三所院校分别承诺使用同一批实训资源，联系人收到互相冲突的跟进邮件，
原始合作意向也无法对齐。本系统用四条不变量消除这些问题（对应领域契约 `domain/contract.json`）：

- **双向匹配**：候选必须同时满足企业需求与院校能力/资源，且双方授权联系人各自独立确认。
- **资源冲突**：候选生成时即对资源容量做预留（held），同一批共享资源不可能被超量重复承诺；
  拒绝/失效释放预留，转项目转为承诺占用（committed），终止才释放。
- **联系人授权**：联系人可按需求授权，失效后其名下未签约候选自动过期。
- **转换谱系**：每个状态变化写入时间线；转项目时冻结需求版本、撮合理由、双边确认与资源占用快照。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/matchmaking/`：撮合后端
  - `models.py`：领域模型（机构、联系人、能力、资源/占用、排除条件、意向、需求版本、候选、项目、时间线）。
  - `engine.py`：硬条件过滤、评分理由、容量预留、候选存活重评估。
  - `service.py`：用例编排与转换谱系（全部写操作加锁并落盘）。
  - `repository.py`：内存仓储 + JSON 文件持久化（原子写入）。
  - `api.py`：基于标准库的 HTTP/JSON API。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/demo.py`：端到端场景演示（三院校与同一批资源）。
- `tests/`：契约回归、领域场景与 HTTP API 测试。

## 候选与项目生命周期

```
意向 active ──撤回──> withdrawn（名下未签约候选全部 expired）
需求 v1 active ──修订──> v1 superseded + v2 active（旧版本候选 expired，按新版本重撮合）
候选 pending ──企业确认──> pending ──院校确认──> confirmed
     ├─ 任一方拒绝 ──> rejected（释放预留；当前需求版本内不再推荐该院校）
     ├─ 撤回/联系人失效/需求变更/新增排除/资源占用 ──> expired（释放预留）
     └─ confirmed ──转项目──> converted（冻结依据）
项目 negotiating ──签约──> active ──终止(带原因)──> terminated（释放承诺容量）
```

重评估由写操作自动触发（联系人增删、排除条件、需求修订、意向/需求撤回、资源占用/容量下调、
能力/资源新增），也可用 `POST /matching/run` 全量执行。硬性条件未通过的配对会记录原因码
（`exclusion`、`enterprise_contact_unauthorized`、`college_contact_unauthorized`、
`capability_mismatch`、`insufficient_capacity`），可在追溯接口中查看。

## HTTP API

启动：`python3 tools/serve.py --port 8080 --data data/forum.json`（省略 `--data` 则纯内存）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/organizations` | 登记机构（kind: forum/college/enterprise） |
| POST | `/contacts` | 新增授权联系人（可带 `authorized_demand_ids` 限定范围） |
| POST | `/contacts/{id}/invalidate` | 联系人失效（自动重评估） |
| POST | `/capabilities` | 院校登记能力（类别 + 标签） |
| POST | `/resources` | 登记资源批次与容量（院校自有或论坛共享池） |
| POST | `/resources/{id}/occupy` | 资源被外部正式占用（committed） |
| POST | `/resources/{id}/reduce` | 容量下调（设备故障/场地改造，自动重评估） |
| POST | `/exclusions` | 新增双方排除条件（自动重评估） |
| POST | `/intents` | 企业登记最初合作意向 |
| POST | `/intents/{id}/withdraw` | 撤回意向（自动重评估） |
| POST | `/demands` | 在意向下创建需求（首版本 v1，自动撮合） |
| POST | `/demands/{id}/revise` | 需求变更（封存旧版本，按新版本重撮合） |
| POST | `/demands/{id}/withdraw` | 撤回需求 |
| POST | `/matching/run` | 全量撮合/重评估 |
| GET | `/candidates?demand_id=&status=` | 查询候选（含 `reasons` 与评分） |
| POST | `/candidates/{id}/confirm` | 一方独立确认（`party`: enterprise/college） |
| POST | `/candidates/{id}/reject` | 一方拒绝 |
| POST | `/candidates/{id}/convert` | 双方确认后转正式项目（冻结依据） |
| POST | `/projects/{id}/sign` | 签约 |
| POST | `/projects/{id}/terminate` | 终止（带原因，释放容量） |
| GET | `/demands/{id}/timeline` | 单条需求的完整时间线 |
| GET | `/intents/{id}/trace` | 从意向到签约/终止的完整谱系 |

业务错误返回 JSON：`{"error": "...", "code": "VALIDATION_ERROR|NOT_FOUND|CONFLICT|ILLEGAL_STATE"}`，
对应 HTTP 400/404/409。

## 验证

```bash
# 全部测试（契约 + 领域场景 + HTTP API）
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tools tests

# 契约摘要
python3 tools/check_contract.py domain/contract.json

# 场景演示
python3 tools/demo.py
```
