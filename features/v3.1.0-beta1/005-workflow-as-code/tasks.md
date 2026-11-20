# Tasks: 工作流即代码（YAML 声明 + CLI 命令族 + 节点模板后端化）

**关联规格**: [spec.md](./spec.md) · [design.md](./design.md)
**版本**: v3.1.0-beta1

---

## 状态

| 步骤 | 状态 | 备注 |
|------|------|------|
| spec.md | ✅ 已评审 | 44 条 AC，EARS 句型；2026-11-20 用户确认 |
| design.md | ✅ 已评审 | 9 条决策 + 23 条坑 + AC 落点索引；2026-11-20 用户确认 |
| tasks.md | ✅ 已拆解 | 47 个任务，7 个 Wave；2026-11-20 `/sdd-review tasks` 通过 |
| 实现 | 🔄 进行中 | Wave 1 完成（T001–T006，6 / 47）。偏差处理见 design.md 顶部调整原则 + `docs/SDD-Guide.md` §3-§4 |

---

## 开发模式

**按 Wave 组织任务**：无依赖的归 Wave 1（可并行），依赖前序的归后续 Wave。每个任务标 `依赖:`。

**后端 Test-First（严格）**：后端侧每个实现任务都有一个**编号更小的**配对测试任务，测试先行。测试用 `src/backend/test/conftest.py` 既有 fixture（`test_client` / `db_session` / `tenant_context` / `mock_openfga` / `mock_settings`），**不新建 conftest**。

**CLI 侧 Test-First（同样严格）**：每个实现任务配一个编号更小的测试任务。CLI 测试的三条铁律（`src/bisheng-cli/tests/conftest.py` 文件头）继续适用——不碰网络 / 不碰真实 home / 不写长 key 字面量；`no_network` 哨兵会抓住任何未 mock 的网络调用。

**前端**：手动验证（每个任务附验证步骤）。前端不做 Playwright 自动化（`docs/SDD-Guide.md` §6 尚在规划中）。

**自包含任务**：每个任务内联文件路径、逻辑、测试上下文；设计论证指向 design §X，不复制。

**无测试配对的任务**（按仓库既有惯例）：前端任务（T042 / T043 / T044）用**手动验证**；基础设施与文档任务（T015 / T047）由**既有测试**兜底（契约一致性测试、release-contract 归档）。其余每个【实现】任务都有一个编号更小的【测试】任务配对。

### 本 feature 的特殊约定

| 约定 | 说明 |
|---|---|
| **无数据库模型变更** | 不改表结构、不加列、不加 DAO 入口。写路径全走既有 DAO（`FlowVersionDao.create_version` / `change_current_version`）。因此**无数据库回滚方案**（不存在需要回滚的 schema 变更）——这是设计结果，不是遗漏（design §2 C1/C2） |
| **无 Worker / Celery 变更** | 不新增 Celery 任务、不改已有任务入参。写路径是同步 HTTP；运行走既有 `POST /api/v2/workflow/invoke`。因此**无 tenant_id 的 headers → ContextVar 传递问题**（既有异步腿由 `OpenApiExecutionSnapshot.from_principal` 负责，本 feature 不改） |
| **无新后端错误码** | 既有 105 段已覆盖全部要报的场景（design §6.3）。若实现中发现既有码都不表达某个意思，**必须停下**在 design 里补一个码并同步三语文案（`src/frontend/packages/locales/src/api_errors/`），不得在代码里临时造一个未注册的码 |
| **依赖预算** | CLI 侧**不新增任何第三方依赖**（PyYAML 已在）。后端侧若需新依赖，必须在 design 里补理由并同步 `pyproject.toml` |
| **scope 与端点必须同一提交** | `open_api_scope` 装饰器在 scope 未注册时于 **import 期**抛 `ValueError`（`scopes.py:280`）。T008（注册表）与引用它的端点任务必须在同一次提交里落地 |
| **共享文件的影响范围** | ① `open_api/domain/scopes.py` —— scope 注册表；`workflow:read` 的 `endpoints` 元组是**声明性**的，实际强制在端点的装饰器上，两处都要同步。<br>② `api/router.py` —— v2 路由挂载点，T006 / T010 / T012 各自**追加**一个 router，不改既有行。<br>③ `bisheng_cli/errors.py` —— CLI 错误码表，**只增不改**（既有映射被别的命令依赖）。<br>④ `commands/flow.py` —— 各子命令分支**逐个追加**，不重写已存在的分支。 |
| **客户契约必须同步** | `test/open_api/test_openapi_schema_contract.py::test_generated_customer_contract_matches_registered_v2_http_routes` 断言 **v2 路由集与客户契约文档的端点集完全相等**。新增任何 v2 端点而不刷新契约文件，这条既有测试立刻红。见 T015 |
| **前端分区** | 本 feature 只碰 **Platform**（`src/frontend/platform/`）。`src/frontend/client/` **不动**（客户端不消费节点模板，见 design §3 决策 2） |

### 任务类别分布

| 类别 | 任务 |
|---|---|
| 基础设施 | T008（scope 注册表）、T015（客户契约刷新）、T041（CLI 错误码映射） |
| 后端 Domain Service | T002（节点模板模块）、T014（工具列表增量字段） |
| 后端 API 层 | T004、T006、T010、T012 |
| 前端 Platform | T042、T043、T044、T045 |
| 前端 Client | **无**（不适用） |
| Worker | **无**（不适用——本 feature 不新增 Celery 任务） |

---

## Tasks

### Wave 1 — 后端：节点模板底座

> 这一波把"节点模板只有一份且由后端持有"落地（design 决策 2）。它是后面所有切片的地基：CLI 编译要取模板，前端要取模板，工具节点模板也要后端生成。

- [x] **T001**: 【测试】节点模板的黄金对照测试
  **文件**: `src/backend/test/workflow/test_node_templates.py`
  **逻辑**: 从前端源文件 `src/frontend/platform/src/controllers/API/workflow.ts` 的 `workflowTemplate` 字面量（第 185–1077 行）解析出黄金数据，与后端 `bisheng.workflow.common.node_templates` 逐字段对照：12 类节点齐全（start / end / input / output / llm / agent / qa_retriever / rag / knowledge_retriever / report / code / condition）、每类的 `group_params` 结构与参数键逐字一致、`v` 版本号一致。把黄金数据**固化在本测试文件内**（一份字面量，作为迁移的冻结快照）；测试断言后端模块 == 固化值。另加一条 `test_matches_frontend_source_when_available`：前端 `workflow.ts` 还在时与它逐字段比对，**已被 T042 删除则 skip**（迁移完成后前端不再是真相源，这条用例自动退场）。同样地断言 `get_tool_node_template(tool_info)` 的产出与前端 `util/flowUtils.ts` 的 `getToolTree`（第 79–119 行）逐字段相等——**用同一份前端源码当两个被比较方的参照**
  **测试**: `test_templates_cover_all_twelve_types` / `test_template_group_params_match_golden` / `test_matches_frontend_source_when_available` / `test_tool_node_template_contract_is_frozen` / `test_template_carries_no_ui_copy`（断言所有 `label` / `description` 是 `"true"` 占位符或空，不含界面文案）
  **覆盖 AC**: AC-01, AC-03, AC-04
  **依赖**: 无

- [x] **T002**: 【实现】节点模板模块
  **文件**: `src/backend/bisheng/workflow/common/node_templates.py`
  **逻辑**: `NODE_TEMPLATES: list[dict]` —— 12 类节点模板，从前端那份 TS 字面量**逐字**搬过来（不重写、不"顺手整理"）；`get_tool_node_template(tool_info) -> dict` —— 由工具信息生成工具节点模板（两组 `group_params`："工具参数" + "输出"），逻辑照前端 `getToolTree` 的形状。**不引入任何界面文案**（模板只发结构与参数 key）
  **测试**: T001 全部通过
  **覆盖 AC**: AC-01, AC-03, AC-04, AC-44
  **依赖**: T001

- [x] **T003**: 【测试】v1 节点模板端点测试
  **文件**: `src/backend/test/workflow/test_node_templates_api.py`
  **逻辑**: 用 `test/conftest.py` 的 `test_client` fixture 打 `GET /api/v1/workflow/node_templates`；断言：返回 12 类模板；每类含 `type` 与 `group_params`；响应里**没有任何界面文案**；未登录时 401
  **测试**: `test_returns_twelve_templates` / `test_payload_has_no_ui_copy` / `test_requires_login`
  **覆盖 AC**: AC-01, AC-03, AC-05
  **依赖**: T002

- [x] **T004**: 【实现】v1 节点模板端点
  **文件**: `src/backend/bisheng/api/v1/workflow.py`
  **逻辑**: 在既有 `router`（prefix `/workflow`）上加 `GET /node_templates`，鉴权用 `UserPayload.get_login_user`（JWT cookie），返回 `resp_200(data=NODE_TEMPLATES)`。**既有端点一行不改**
  **测试**: T003 全部通过
  **覆盖 AC**: AC-01, AC-03
  **依赖**: T003

- [x] **T005**: 【测试】v2 节点模板端点测试
  **文件**: `src/backend/test/open_api/test_flow_node_templates_v2.py`
  **逻辑**: 打 `GET /api/v2/flow/node_templates`。断言：带 `workflow:read` 的凭据拿到与 v1 **逐字段相同**的 12 类模板；缺 scope 时被拒（不返回数据）；端点带 scope 标记（`get_open_api_scope_marker`）
  **测试**: `test_same_payload_as_v1` / `test_missing_scope_is_refused` / `test_endpoint_carries_scope_marker`
  **覆盖 AC**: AC-01, AC-05
  **依赖**: T002

- [x] **T006**: 【实现】v2 节点模板端点 + 路由挂载
  **文件**: `src/backend/bisheng/open_endpoints/api/endpoints/flow_templates.py`（新）、`src/backend/bisheng/api/router.py`
  **逻辑**: 新建 router（prefix `/flow`），端点 `GET /node_templates` 挂 `@open_api_scope("workflow:read")`，数据源与 v1 端点**同一个** `NODE_TEMPLATES`；在 `api/router.py` 把它挂到 `router_rpc`（与 `flow_router_rpc` 同级）。**端点不得自行解析身份**（走 `get_current_open_api_principal()`）
  **测试**: T005 全部通过
  **覆盖 AC**: AC-01
  **依赖**: T005

### Wave 2 — 后端：写路径、工具面与客户契约

> 这一波建"写"的通道（design 决策 3）。关键点：**业务逻辑一律复用既有 `WorkFlowService`，不新写授权逻辑**；身份用 `get_open_api_operator_async()` 桥接。

- [ ] **T007**: 【测试】scope 注册表测试
  **文件**: `src/backend/test/open_api/test_scopes.py`
  **逻辑**: 断言 ① `workflow:write` 在 `OPEN_API_SCOPES` 里存在，其 `group` 是 `"app"`（**不是** `local_dev_toolkit`）、`label_key` / `desc_key` 前缀是 `openApiManagement.scopes.`；② `workflow:read` 的 `endpoints` 元组已含四条新读端点；③ `workflow:write` 的 `endpoints` 元组覆盖六条写端点路径；④ 元组里的每条路径**都有实际注册的路由**（比对 `app.routes`，`delegate` / `identity:read` 除外——这两个本来就没有路由）；⑤ 装饰器 `@open_api_scope("workflow:write")` 能解析（不抛 `ValueError`）
  **测试**: `test_workflow_write_scope_registered` / `test_workflow_read_scope_extended` / `test_scope_endpoints_match_registered_routes` / `test_scope_group_is_not_local_dev_toolkit`
  **覆盖 AC**: AC-38
  **依赖**: 无

- [ ] **T008**: 【实现】scope 注册
  **文件**: `src/backend/bisheng/open_api/domain/scopes.py`
  **逻辑**: ① **扩展**既有 `workflow:read` 的 `endpoints` 元组，追加 `("GET", f"{_V2}/flow/node_templates")`、`("GET", f"{_V2}/flows/{{flow_id}}/versions")`、`("GET", f"{_V2}/flows/{{flow_id}}/versions/{{version_id}}")`、`("GET", f"{_V2}/tools")`；② 新增 `OpenApiScope("workflow:write", …)`，覆盖 `POST /api/v2/flows`、`POST /api/v2/flows/{flow_id}/versions`、`PUT /api/v2/flows/{flow_id}/versions/{version_id}`、`POST /api/v2/flows/{flow_id}/versions/{version_id}/activate`、`POST /api/v2/tools`、`POST /api/v2/tools/schema`，`group="app"`。**必须与引用它的端点任务在同一提交里**（装饰器在 import 期校验）。既有 scope 条目**一行不改**（只加 `workflow:read` 的四条端点）
  **测试**: T007 全部通过
  **覆盖 AC**: AC-38, AC-44
  **依赖**: T007

- [ ] **T009**: 【测试】v2 工作流端点的权限双层测试
  **文件**: `src/backend/test/open_api/test_workflow_write_v2.py`
  **逻辑**: 用 `test/conftest.py` 的 `test_client` + `mock_openfga` + `tenant_context` fixture。覆盖：<br>① 缺 `workflow:write` scope → 拒绝（不建版本）；<br>② 凭据**无资源归属人** → 拒绝（不得当成"对任何工作流都有权"）；<br>③ 归属人对该工作流**无 edit 权限** → 拒绝；<br>④ `hosted_app` 主体 → 拒绝；<br>⑤ 正常路径：建版本成功、版本 `data` 等于提交值、**既有版本的 `data` 未被改写**、切当前版本后 `Flow.data` 等于新版本 data；<br>⑥ 工作流处于**上线**状态时切当前版本 → 10525；<br>⑦ 响应里**没有任何"指定他人身份"的入参**（请求体多传身份字段被拒）；<br>⑧ 读端点（`GET /versions`、`GET /versions/{id}`）只需 `workflow:read`，无需 `workflow:write`
  **测试**: `test_missing_scope_refused` / `test_no_resource_owner_refused` / `test_non_owner_edit_refused` / `test_hosted_app_refused` / `test_create_version_does_not_touch_existing_versions` / `test_activate_updates_flow_data` / `test_activate_online_workflow_returns_10525` / `test_no_impersonation_entry_point` / `test_read_endpoints_need_only_read_scope`
  **覆盖 AC**: AC-25, AC-27, AC-28, AC-29, AC-39, AC-40, AC-42, AC-43
  **依赖**: T008

- [ ] **T010**: 【实现】v2 工作流端点 + 路由挂载
  **文件**: `src/backend/bisheng/open_endpoints/api/endpoints/workflow_write.py`（新）、`src/backend/bisheng/api/router.py`
  **逻辑**: router（prefix `/flows`）。端点：<br>**读**（`@open_api_scope("workflow:read")`）：`GET /{flow_id}/versions`、`GET /{flow_id}/versions/{version_id}`；<br>**写**（`@open_api_scope("workflow:write", modes=("S",))`，**仅服务账号模式**，不给"代他人写"的入口）：`POST /`、`POST /{flow_id}/versions`、`PUT /{flow_id}/versions/{version_id}`、`POST /{flow_id}/versions/{version_id}/activate`。<br>身份从 `get_current_open_api_principal()` 读，操作者用 `await get_open_api_operator_async()` 取（服务账号拿到的 `UserPayload` 角色为空、非管理员）。**业务逻辑一律委托既有 `WorkFlowService`（`api/services/flow.py`）**：`judge_flow_write_permission` / `create_new_version` / `change_current_version` / `update_version_info`。**不新写授权逻辑、不 import `database.models`、不新加 DAO 入口**
  **测试**: T009 全部通过
  **覆盖 AC**: AC-25, AC-26, AC-27, AC-28, AC-29, AC-39, AC-40, AC-42, AC-43
  **依赖**: T009

- [ ] **T011**: 【测试】v2 工具端点测试
  **文件**: `src/backend/test/open_api/test_tools_v2.py`
  **逻辑**: 覆盖：`POST /api/v2/tools/schema` 把 OpenAPI 描述解析成工具类型（每个 operation 成为一个 child，`tool_key` 非空）；`POST /api/v2/tools` 注册成功；`GET /api/v2/tools` 列出（只需 `workflow:read`）；**缺 `workflow:write` scope 时注册被拒**（不返回成功）；**缺 operationId 时注册失败并说明缺哪一项**（operationId 是工作流引用工具的唯一依据）
  **测试**: `test_schema_parses_operations` / `test_register_creates_tool_type` / `test_list_returns_registered_tools` / `test_missing_scope_refused` / `test_missing_operation_id_refused` / `test_list_needs_only_read_scope`
  **覆盖 AC**: AC-34, AC-35, AC-36, AC-37
  **依赖**: T008

- [ ] **T012**: 【实现】v2 工具端点 + 路由挂载
  **文件**: `src/backend/bisheng/open_endpoints/api/endpoints/tools_v2.py`（新）、`src/backend/bisheng/api/router.py`
  **逻辑**: router（prefix `/tools`）。`GET /`（列出，挂 `workflow:read`）、`POST /`（注册，挂 `workflow:write, modes=("S",)`）、`POST /schema`（解析 OpenAPI，挂 `workflow:write, modes=("S",)`）。**复用既有 `ToolServices`（`bisheng.tool.domain.services.tool`）的 `parse_openapi_schema` / `add_tools`**，不 import `bisheng.tool.api.tool`（那会命中 arch-guard RULE-5 的跨模块 API 导入）
  **测试**: T011 全部通过
  **覆盖 AC**: AC-34, AC-35, AC-36, AC-37
  **依赖**: T011

- [ ] **T013**: 【测试】工具列表的 `node_template` 字段测试
  **文件**: `src/backend/test/workflow/test_tool_node_template.py`
  **逻辑**: 断言 `GET /api/v1/tool` 的每个 child **新增**了 `node_template` 字段，且它等于 `get_tool_node_template(child)` 的产出；**既有字段一字未变**（`name` / `desc` / `api_params` / `tool_key` 仍在原位）
  **测试**: `test_each_child_carries_node_template` / `test_existing_child_fields_unchanged` / `test_node_template_matches_generator`
  **覆盖 AC**: AC-04
  **依赖**: T002

- [ ] **T014**: 【实现】工具列表的 `node_template` 字段
  **文件**: `src/backend/bisheng/tool/domain/services/tool.py`
  **逻辑**: 在 `get_tool_list` 组装 children 的地方，**附加**（不改、不删）`node_template=get_tool_node_template(child)`。纯增量字段
  **测试**: T013 全部通过
  **覆盖 AC**: AC-04
  **依赖**: T013

- [ ] **T015**: 【实现】刷新开放 API 客户契约
  **文件**: `features/v3.0.0-beta1/053-openapi-auth-and-identity/openapi-v2-key-auth-api.json`、同目录的 `openapi-v2-key-auth-api.md` 与 `openapi-v2-key-auth-api-by-category.md`
  **逻辑**: 跑 `python features/v3.0.0-beta1/053-openapi-auth-and-identity/generate_openapi_contract.py` 重新生成 JSON（该脚本会强制打开 `open_platform.enabled` 再 import app，因此产出的是完整面）。再按新端点补齐两份人读 md 里按 scope 罗列端点的部分。**这是新增 v2 端点的必要配套**——`test/open_api/test_openapi_schema_contract.py::test_generated_customer_contract_matches_registered_v2_http_routes` 断言 v2 路由集与契约文档端点集**完全相等**，不刷新这条既有测试立刻红
  **测试**: `cd src/backend && uv run pytest test/open_api/test_openapi_schema_contract.py -v` 全部通过
  **覆盖 AC**: AC-01, AC-25, AC-27, AC-29, AC-34, AC-36
  **依赖**: T010, T012

### Wave 3 — CLI：声明加载、覆盖、编译、校验

> 这一波把编译器建起来（design 决策 1、6、7）。CLI 侧**零新增依赖**，YAML 用 `SafeLoader` 子类。

- [ ] **T016**: 【测试】CLI 命令族契约测试
  **文件**: `src/bisheng-cli/tests/test_cli.py`
  **逻辑**: 参数化 over 全部已注册命令的每个叶子：① 全局 flag（`--json` / `--verbose` / `--quiet` / `--timeout` / `--no-proxy` / `--platform`）都镜像到了叶子；② `bisheng --json <cmd>` 与 `bisheng <cmd> --json` 两种顺序解析出**同一个** `json_mode`；③ `SUBCOMMANDS` 里没有桩；④ 每条退出路径恰好发出一个 `result` 事件
  **测试**: `test_every_leaf_mirrors_global_flags` / `test_flag_order_is_equivalent` / `test_no_subcommand_is_a_stub` / `test_exactly_one_result_event`
  **覆盖 AC**: AC-23
  **依赖**: 无

- [ ] **T017**: 【实现】CLI `flow` 命令族接入（分发骨架）
  **文件**: `src/bisheng-cli/bisheng_cli/cli.py`、`src/bisheng-cli/bisheng_cli/main.py`、`src/bisheng-cli/bisheng_cli/commands/flow.py`（新）
  **逻辑**: `cli.py` 的 `SUBCOMMANDS` 加 `"flow"`；`flow` 是**两 token 命令**（照 `skills` / `platforms` 的形状）：`flow` 自己的 subparser 携带动词，每个动词叶子再镜像一遍全局 flag（`default=argparse.SUPPRESS`）；`main.py` 的 `_HANDLER_PATHS` 加 `"flow": "bisheng_cli.commands.flow:run"`；`commands/flow.py` 实现分发。**此时只注册已实现的子命令**（上游铁律：命令要么注册要么不存在，绝不注册桩）——本任务只接通 `compile` 的分发路径，其余动词在后续任务里追加。三处必须同时改，否则命令注册不完整
  **测试**: T016 全部通过
  **覆盖 AC**: AC-23
  **依赖**: T016

- [ ] **T018**: 【测试】YAML 加载层测试
  **文件**: `src/bisheng-cli/tests/test_flow_spec.py`
  **逻辑**: 覆盖：① 含 `!!python/object` 标签的声明文件**被拒**（照 `project.py` 的 `yaml.safe_load` 做法，不得执行）；② `!include` 展开为文件内容（路径相对**声明文件所在目录**，不是 CWD）；③ 缺失必填顶层字段 → 本地拒绝并指出缺哪一项，**且不发起任何平台请求**（`no_network` 哨兵会抓住）；④ 顶层不是键值映射 → 拒绝；⑤ `!include` 指向缺失文件 → 报错并给出解析后的路径
  **测试**: `test_python_object_tag_is_rejected` / `test_include_expands_relative_to_spec_dir` / `test_missing_top_level_field_rejected_without_request` / `test_non_mapping_top_level_rejected` / `test_missing_include_file_reports_path`
  **覆盖 AC**: AC-07, AC-10, AC-11
  **依赖**: T017

- [ ] **T019**: 【实现】YAML 加载层
  **文件**: `src/bisheng-cli/bisheng_cli/flow/spec.py`
  **逻辑**: 定义 `SafeLoader` 子类并注册**唯一**一个自定义构造器 `!include`（只读文本文件、返回内容；**不 eval、不查类、不导入模块**）；`load_spec(path) -> dict` 做本地快检：可安全加载、顶层是映射、必填顶层字段齐全。**不填默认值、不裁定节点类型合法性**（那是服务端的事）
  **测试**: T018 全部通过
  **覆盖 AC**: AC-07, AC-10, AC-11
  **依赖**: T018

- [ ] **T020**: 【测试】受限覆盖层测试
  **文件**: `src/bisheng-cli/tests/test_flow_overlay.py`
  **逻辑**: 允许：覆盖 YAML 已声明节点的**已声明参数**取值、追加 YAML 未声明的**节点**、追加 YAML 未声明的**连线**。拒绝（并**逐项列出被拒项**：哪条声明的哪个参数、哪条已声明的节点/连线）：删除节点、删除连线、改一条已声明连线的 `sourceHandle` / `target`、改已声明节点的 `id` / `type`、新增 YAML 未声明的参数键
  **测试**: `test_can_override_declared_param` / `test_can_append_undeclared_node` / `test_can_append_undeclared_edge` / `test_delete_node_refused_and_itemised` / `test_delete_edge_refused_and_itemised` / `test_change_edge_handle_refused` / `test_change_node_id_refused` / `test_new_undeclared_param_refused`
  **覆盖 AC**: AC-08, AC-09
  **依赖**: T019

- [ ] **T021**: 【实现】受限覆盖层
  **文件**: `src/bisheng-cli/bisheng_cli/flow/overlay.py`
  **逻辑**: `apply_overlay(spec, overlay_module) -> dict`。覆盖层是一个 Python 文件，导出 `apply(spec)`；执行它（用户显式指定 `--overlay` 时执行自己的代码——这是**唯一会被执行的代码**，YAML 永远不执行）。应用结果按 T020 的语义表校验，违规**逐项报错**。**限制的是它对工作流结构的影响力，不是它的代码能力**（文档必须说清这一点）
  **测试**: T020 全部通过
  **覆盖 AC**: AC-08, AC-09
  **依赖**: T020

- [ ] **T022**: 【测试】编译器测试
  **文件**: `src/bisheng-cli/tests/test_flow_compiler.py`
  **逻辑**: 覆盖：各节点类型产出**完整**的 node 骨架（`id` / `type:"flowNode"` / `position` / `data`，`data` 里的 `group_params` 结构来自取到的模板）；`params` 里声明的键被填进对应分组对应参数；工具节点按 `tool: <operationId>` 解析出 `tool_key` 并生成工具模板骨架；**工具引用解析不到时报错**（不产出缺 `tool_key` 的节点）；`!include` 展开的提示词进入 `system_prompt` / `user_prompt`；产出 `{nodes, edges, viewport}` 三部分
  **测试**: `test_each_node_type_produces_full_skeleton` / `test_params_filled_into_matching_group` / `test_tool_node_resolves_tool_key` / `test_unknown_operation_id_is_error` / `test_included_prompt_expanded` / `test_output_has_nodes_edges_viewport`
  **覆盖 AC**: AC-06
  **依赖**: T019, T021, T014

- [ ] **T023**: 【实现】编译器
  **文件**: `src/bisheng-cli/bisheng_cli/flow/compiler.py`
  **逻辑**: `compile_spec(spec, templates, tools) -> dict`。流程：按 `type` 深拷贝节点模板骨架 → 把声明的 `params` 填进匹配分组的匹配参数（**按 key 匹配，不按位置**）→ 工具节点用后端提供的工具模板（由 v2 工具端点的 `node_template` 字段带回）→ 生成 edges（`id` / `source` / `sourceHandle` / `target` / `targetHandle` / `animated` / `type`）→ 产出 `{nodes, edges, viewport}`
  **测试**: T022 全部通过
  **覆盖 AC**: AC-06
  **依赖**: T022

- [ ] **T024**: 【测试】编译期结构校验测试（双向用例）
  **文件**: `src/bisheng-cli/tests/test_flow_validate.py`
  **逻辑**: **每条规则两条用例**——"该报的报出来" + "不该报的不误报"（只写正例的测试等于没测）。12 条规则：① 缺节点 ② 缺 start ③ 节点 id 空 / 重复 ④ 未知节点类型 ⑤ 非 end 节点无连线 ⑥ 边指向不存在的节点 ⑦ `sourceHandle` 不是该节点的合法出口（condition 的 case id 或 `right_handle`）⑧ 变量引用格式错 / 引用到的节点不存在 ⑨ 缺该节点类型的必填参数（**按 design §4.4 的必填参数表逐类型查**，`output` 节点的 `output_msg`/`message` 二选一、`agent` 的 `sql_agent` 条件必填）⑩ `batch_variable` 与 `output[i].key` 不一一对应 ⑪ `tool_key` 解析不到 ⑫ `condition` 数组 / case / 条件的必填项缺失。每条报错必须**可定位**（哪个节点、哪个参数、哪个引用）
  **测试**: 每个规则一个 `test_reports_<rule>` + 一个 `test_accepts_valid_<rule>`
  **覆盖 AC**: AC-12, AC-13, AC-14, AC-15, AC-16, AC-17, AC-18
  **依赖**: T023

- [ ] **T025**: 【实现】编译期结构校验
  **文件**: `src/bisheng-cli/bisheng_cli/flow/validate.py`
  **逻辑**: `validate(compiled) -> list[Issue]`，12 条规则按 design §4.4 实现；每条 Issue 带可定位信息（节点 id / 参数键 / 引用 / 连线两端与句柄）。**只做"确定无疑"的检查**——不判断"某参数在平台上是否参与执行"，不判断"某资源 id 是否还有效"（design §5 坑 21）
  **测试**: T024 全部通过
  **覆盖 AC**: AC-12, AC-13, AC-14, AC-15, AC-16, AC-17, AC-18
  **依赖**: T024

### Wave 4 — CLI：客户端层与 diff

- [ ] **T026**: 【测试】CLI 客户端层测试
  **文件**: `src/bisheng-cli/tests/test_flow_client.py`
  **逻辑**: 覆盖：模板取回与**缓存**（命中缓存不发请求、缓存失效时重取、**取不到时失败且不回落任何硬编码副本**）；工具列表取回与 `operationId → tool_key` 解析；版本读取（默认基准 = 当前版本、指定版本 = 历史版本）；**线上不存在 / 无权读取 / 读取失败三种情形分别给出可行动的原因**（不报"无差异"）；平台错误码 → `CliError` 的映射（`details` / `hints` 原样透传）；**输出里不出现凭据明文**
  **测试**: `test_templates_cached_and_invalidated` / `test_template_fetch_failure_has_no_fallback` / `test_operation_id_resolves_to_tool_key` / `test_missing_workflow_distinguished_from_forbidden` / `test_platform_error_becomes_clierror` / `test_no_credential_in_output`
  **覆盖 AC**: AC-05, AC-24, AC-28, AC-41
  **依赖**: T006, T012, T014

- [ ] **T027**: 【实现】CLI 客户端层
  **文件**: `src/bisheng-cli/bisheng_cli/flow/client.py`
  **逻辑**: 用既有 `PlatformClient`（`bisheng_cli/http.py`）封装 v2 调用：取节点模板（带按 `base_url` 的本地缓存，缓存是**性能优化不是副本**，取不到就失败）、取工具列表、读版本、建版本、切当前版本、注册工具、invoke。平台错误码经 `errors.py` 翻成 `CliError`（`as_failure()` 的五元组）。**不做权限判定**
  **测试**: T026 全部通过
  **覆盖 AC**: AC-05, AC-24, AC-28, AC-41
  **依赖**: T026

- [ ] **T028**: 【测试】diff 算法测试（双向探针）
  **文件**: `src/bisheng-cli/tests/test_flow_diff.py`
  **逻辑**: **必须报出来**：线上独有节点、本地独有节点、同一节点上取值不同的参数、线上独有连线、本地独有连线。**必须不算差异**：仅 `position` 不同、仅 `viewport` 不同、仅 `data.v` 不同、仅参数的 `label` / `desc` / `type` / `required` 不同。**必须算差异**：`tab.value` 不同、`tool_key` 不同、`name` 不同。参数比较**按 key 不按位置**（加一个参数不应让其余参数全被报成"变了"）。差异项必须可定位（节点 id / 参数键 / 连线两端与句柄）
  **测试**: `test_reports_node_only_online` / `test_reports_node_only_local` / `test_reports_changed_param` / `test_reports_edge_only_online` / `test_reports_edge_only_local` / `test_position_only_is_not_a_diff` / `test_viewport_only_is_not_a_diff` / `test_node_version_only_is_not_a_diff` / `test_param_label_only_is_not_a_diff` / `test_tab_value_is_a_diff` / `test_tool_key_is_a_diff` / `test_params_compared_by_key_not_position`
  **覆盖 AC**: AC-19, AC-20, AC-21, AC-22, AC-23
  **依赖**: T023

- [ ] **T029**: 【实现】diff 算法
  **文件**: `src/bisheng-cli/bisheng_cli/flow/diff.py`
  **逻辑**: `diff(local, online) -> dict`，按 design 决策 5 的比较规则表实现：节点按 `id` 索引；参数按 `group_params[].params[].key` 展平比 `value`；节点其他字段比 `name` / `description` / `tab` / `tool_key`；连线按 `(source, sourceHandle, target)`；排除 `position` / `viewport` / `data.v` / 参数元数据。输出机器可读且**人读呈现与机器可读呈现内容一致**
  **测试**: T028 全部通过
  **覆盖 AC**: AC-19, AC-20, AC-21, AC-22, AC-23
  **依赖**: T028

### Wave 5 — CLI：命令接线（compile / diff / push / run / tool）

> 每个任务**追加**一个子命令分支到 `commands/flow.py`，不重写已存在的分支。

- [ ] **T030**: 【测试】`flow compile` 命令测试
  **文件**: `src/bisheng-cli/tests/test_flow_command_compile.py`
  **逻辑**: 覆盖：合法声明 → 输出 `{nodes, edges, viewport}`；`--json` 时 NDJSON 走 stdout、人读文本走 stderr、**最后一行恒为 `result` 事件**；校验失败 → 退出码与可读原因（**不是堆栈**），`details` 里带全部问题项；`--overlay` 生效；`--out FILE` 写出结果
  **测试**: `test_compile_emits_definition` / `test_json_mode_splits_streams` / `test_result_event_is_last_line` / `test_validation_failure_reports_all_issues` / `test_overlay_applied` / `test_out_file_written`
  **覆盖 AC**: AC-06, AC-09, AC-18, AC-23
  **依赖**: T025, T027

- [ ] **T031**: 【实现】`flow compile` 命令
  **文件**: `src/bisheng-cli/bisheng_cli/commands/flow.py`
  **逻辑**: `compile` 分支：加载声明 → 应用覆盖层 → 取模板与工具列表 → 编译 → 校验 → 输出（`--json` 走 NDJSON；人读文本走 stderr）。**校验失败时退出码非 0，`details` 带全部问题项**
  **测试**: T030 全部通过
  **覆盖 AC**: AC-06, AC-09, AC-18, AC-23
  **依赖**: T030

- [ ] **T032**: 【测试】`flow diff` 命令测试
  **文件**: `src/bisheng-cli/tests/test_flow_command_diff.py`
  **逻辑**: 覆盖：默认基准 = 线上当前版本；`--version-id` 指定历史版本为基准；`--list-versions` 列出可用版本；差异输出**机器可读**（`details` 里的节点/参数/连线差异项可定位）；**人读呈现与机器可读呈现的差异内容一致**；线上不存在 / 无权 / 失败三态分别报错（**不报"无差异"**）；工作流 id 绑定写在项目内 `.bisheng/flow.json`（形状照 `.bisheng/app.json`）
  **测试**: `test_defaults_to_current_version` / `test_version_id_selects_historical_baseline` / `test_list_versions` / `test_machine_readable_details` / `test_human_and_machine_agree` / `test_three_failure_modes_distinguished` / `test_flow_binding_written_to_bisheng_dir`
  **覆盖 AC**: AC-19, AC-20, AC-21, AC-22, AC-23, AC-24
  **依赖**: T029, T031

- [ ] **T033**: 【实现】`flow diff` 命令
  **文件**: `src/bisheng-cli/bisheng_cli/commands/flow.py`
  **逻辑**: `diff` 分支：编译本地 → 取线上基准 → 跑 diff → 输出（结构化 + 人读）。`--version-id` / `--list-versions` 走 `GET /api/v2/flows/{flow_id}/versions[/{version_id}]`。**不做权限判定**（服务端说了算）
  **测试**: T032 全部通过
  **覆盖 AC**: AC-19, AC-20, AC-21, AC-22, AC-23, AC-24
  **依赖**: T032

- [ ] **T034**: 【测试】`flow push` 命令测试
  **文件**: `src/bisheng-cli/tests/test_flow_command_push.py`
  **逻辑**: 覆盖：<br>① 推送后**新建**了一个版本，**既有版本的 data 未被改写**；<br>② 推送后**切为当前版本**，`Flow.data` 等于新版本 data；<br>③ 线上有独有内容时：**无 TTY → 拒绝**（不默认同意），要求 `--allow-drop-online-only`；有 TTY → 展示被保留在旧版本里的内容并要求确认；<br>④ 上线状态 → 报出"版本已建，但工作流在线上，需先下线"，**且不报推送成功**；<br>⑤ 权限不足 → 明确报出**缺少哪个权限位**，不退化为只读、不静默跳过；<br>⑥ 成功 → 报出**新版本的标识与版本名**；<br>⑦ `--no-activate` 只建版本不切
  **测试**: `test_push_creates_new_version` / `test_push_activates_new_version` / `test_existing_versions_untouched` / `test_online_only_content_refused_without_tty` / `test_online_only_content_requires_confirmation_with_tty` / `test_online_workflow_reports_offline_hint` / `test_missing_permission_reports_which_scope` / `test_reports_new_version_id_and_name` / `test_no_activate_only_creates_version`
  **覆盖 AC**: AC-25, AC-26, AC-27, AC-28, AC-29
  **依赖**: T033

- [ ] **T035**: 【实现】`flow push` 命令
  **文件**: `src/bisheng-cli/bisheng_cli/commands/flow.py`
  **逻辑**: `push` 分支：编译 → 取当前版本 → diff → 有线上独有内容时按 `cli.py` 的 `confirm()` 语义处理（**无 TTY 时抛 `CliError(EXIT_USAGE)` 要求显式 flag，不默认同意**）→ `POST /api/v2/flows/{flow_id}/versions` 建新版本 → `POST .../activate` 切当前版本 → 报版本 id 与名称。`--no-activate` 只建不切；`--create` 先 `POST /api/v2/flows` 建工作流
  **测试**: T034 全部通过
  **覆盖 AC**: AC-25, AC-26, AC-27, AC-28, AC-29
  **依赖**: T034

- [ ] **T036**: 【测试】`flow run` 命令测试
  **文件**: `src/bisheng-cli/tests/test_flow_command_run.py`
  **逻辑**: 覆盖：事件流被呈现出来；停在等待输入的节点时**报出停在哪个节点与等待什么输入**；续跑时**同时带 `session_id` 与 `message_id`**（缺一不可 —— 有专门的用例断言两个都发出去了）；节点失败时**报出失败节点与失败原因**（不只报"运行结束"）；**无 TTY 时不阻塞等待人工输入**（`--input` / `--input-file` 供一次性输入，跑到下一次等待输入就结束）
  **测试**: `test_events_are_streamed` / `test_reports_paused_node_and_expected_input` / `test_continuation_sends_session_and_message_id` / `test_node_failure_reports_node_and_reason` / `test_no_tty_does_not_block`
  **覆盖 AC**: AC-30, AC-31, AC-32, AC-33
  **依赖**: T027

- [ ] **T037**: 【实现】`flow run` 命令
  **文件**: `src/bisheng-cli/bisheng_cli/commands/flow.py`
  **逻辑**: `run` 分支：调 `POST /api/v2/workflow/invoke`（`workflow:invoke` scope，既有端点），呈现事件流；遇 input 事件时报出节点与待输入内容，交互式读 stdin / 非交互式用 `--input` / `--input-file`；续跑时 body **同时带 `session_id` 与 `message_id`**
  **测试**: T036 全部通过
  **覆盖 AC**: AC-30, AC-31, AC-32, AC-33
  **依赖**: T036

- [ ] **T038**: 【测试】`flow tool` 命令测试
  **文件**: `src/bisheng-cli/tests/test_flow_command_tool.py`
  **逻辑**: 覆盖：注册后**报出工具类型标识与各可用操作的标识**（工作流声明要按 operationId 引用它们）；`list` 报出各工具类型及其可用操作；权限不足时**明确报出权限不足**（**不报注册成功**）
  **测试**: `test_register_reports_tool_and_operation_ids` / `test_list_reports_types_and_operations` / `test_permission_error_not_reported_as_success`
  **覆盖 AC**: AC-34, AC-35, AC-36, AC-37
  **依赖**: T027

- [ ] **T039**: 【实现】`flow tool` 命令
  **文件**: `src/bisheng-cli/bisheng_cli/commands/flow.py`
  **逻辑**: `tool` 分支的两个动词：`register <openapi.yaml>`（调 `POST /api/v2/tools/schema` → `POST /api/v2/tools`，报出工具类型标识与各操作标识）与 `list`（调 `GET /api/v2/tools`，报出各工具类型及其可用操作）
  **测试**: T038 全部通过
  **覆盖 AC**: AC-34, AC-35, AC-36, AC-37
  **依赖**: T038

- [ ] **T040**: 【测试】105xx 错误码 → 退出码映射测试
  **文件**: `src/bisheng-cli/tests/test_errors.py`
  **逻辑**: 断言 `ERROR_EXIT_CODES` 覆盖 10525 / 10529 / 10502 / 10500 / 10543 / 10599 并映射到预期的退出码（`EXIT_PRECHECK_FAILED` / `EXIT_PUBLISH_CONFLICT` / `EXIT_LOCAL_INVALID` / `EXIT_FORBIDDEN`）；断言缺 scope（26003）的既有中文提示仍生效；断言**新增条目没有改动任何既有映射**（别把别的命令的退出码改掉）
  **测试**: `test_flow_error_codes_mapped` / `test_missing_scope_hint_preserved` / `test_existing_mappings_unchanged`
  **覆盖 AC**: AC-28, AC-41
  **依赖**: 无

- [ ] **T041**: 【实现】105xx 错误码 → 退出码映射
  **文件**: `src/bisheng-cli/bisheng_cli/errors.py`
  **逻辑**: 在 `ERROR_EXIT_CODES` **新增** 105 段映射（10525 → `EXIT_PRECHECK_FAILED`；10529 / 10502 → `EXIT_PUBLISH_CONFLICT`；10500 / 10543 → `EXIT_LOCAL_INVALID`；10599 → `EXIT_FORBIDDEN`），并在 `ERROR_HINTS` 补中文说明（**只增不改**既有条目）
  **测试**: T040 全部通过
  **覆盖 AC**: AC-28, AC-41
  **依赖**: T040

### Wave 6 — 前端 Platform

- [ ] **T042**: 【实现】前端节点模板改从接口取
  **文件**: `src/frontend/platform/src/controllers/API/workflow.ts`
  **逻辑**: `getWorkflowNodeTemplate()` 改为 `axios.get('/api/v1/workflow/node_templates')`（走既有 `controllers/API/request` 封装，**不在 store 里直接调 HTTP**）；**删除**文件里那份约 890 行的 `workflowTemplate` 字面量。改前先用 `grep -rn "workflowTemplate" src/` 全量确认没有别的引用。**前提**：T001 已把黄金数据固化进测试文件（否则 T001 的比对会因前端字面量被删而失效）
  **覆盖 AC**: AC-01, AC-02
  **手动验证**:
  - 打开工作流画布（`/build`），节点库侧栏应列出 12 类节点，拖拽可用
  - 打开知识库问答助手的快捷建流，模板能正常取到
  - 后端接口返回 500 时，节点库侧栏给出可读原因（**不回落硬编码副本**）
  **依赖**: T004

- [ ] **T043**: 【实现】前端 `getToolTree` 收口到后端
  **文件**: `src/frontend/platform/src/util/flowUtils.ts`
  **逻辑**: `getToolTree` 的**形状拼装逻辑全部删除**，改为 `children.map(c => c.node_template)`；**返回形状不变**（仍是 `{name, is_preset, children}`），前端 `Sidebar.tsx` 的消费方式不改
  **覆盖 AC**: AC-04
  **手动验证**:
  - 工作流画布的"工具"页签，展开任一自定义工具 → 参数区与输出区与改造前一致
  - 拖一个工具节点进画布 → 节点参数区完整
  **依赖**: T014, T042

- [ ] **T044**: 【实现】新 scope 的界面文案（三语言）
  **文件**: `src/frontend/platform/public/locales/zh-Hans/bs.json`、`src/frontend/platform/public/locales/en-US/bs.json`、`src/frontend/platform/public/locales/ja/bs.json`
  **逻辑**: 在 `openApiManagement.scopes` 下新增 `workflow_write` 的 `label` 与 `desc`（键名与 `scopes.py` 里 `label_key` / `desc_key` 一致：`openApiManagement.scopes.workflow_write.label` / `.desc`）。**三个语言必须同时加**——`pnpm check-i18n` 校验键集对齐（参照语言 zh-Hans）。既有条目不动
  **覆盖 AC**: AC-38
  **手动验证**:
  - 服务账号签发表单里出现"工作流写入"这一项，中/英/日三种语言都显示人话（不是裸键名）
  **依赖**: T008

- [ ] **T045**: 前端回归确认
  **文件**: 无新文件（验证任务）
  **逻辑**: 跑 `pnpm lint` 与 `pnpm typecheck`、`pnpm check-i18n`；确认 `getWorkflowNodeTemplate` 的两个消费点（`pages/BuildPage/flow/Sidebar.tsx`、`pages/KnowledgePage/components/FileUploadStep4.tsx`）走的是同一个接口、拿到的是同一份数据；确认既有工作流在网页里能打开、能跑
  **覆盖 AC**: AC-02, AC-03
  **手动验证**:
  - 两个消费点各自打开一次，比对拿到的模板**完全一致**
  - 打开一条既有工作流，画布正常渲染（模板搬后端后前端行为不变）
  **依赖**: T043, T044

### Wave 7 — 端到端与收尾

- [ ] **T046**: 【测试】端到端联调（network marker）
  **文件**: `src/bisheng-cli/tests/test_flow_e2e.py`
  **逻辑**: 用 `BISHENG_CLI_RUN_NETWORK_TESTS=1` 才跑（默认跳过，`no_network` 哨兵不容许真跑）；对着真平台跑 `compile → diff → push → run` 全链路，断言：新版本建成、既有版本未改写、diff 结果与 push 前一致、run 能跑起来。**前置**：本机毕昇容器跑的是打进镜像的代码，改过后端代码必须先重建镜像
  **测试**: `test_compile_diff_push_run_end_to_end`
  **覆盖 AC**: AC-06, AC-19, AC-25, AC-30
  **依赖**: T041, T045

- [ ] **T047**: 【实现】版本契约登记
  **文件**: `features/v3.1.0-beta1/release-contract.md`
  **逻辑**: 表 1 登记本特性的领域归属（节点模板后端化 + 工作流写开放面 + CLI `flow` 命令族）；表 2 说明"本版本增量未新增或修改全局业务不变量"；表 3 登记依赖；**已分配模块编码段注明"本特性沿用既有 105 段，未新增错误码"**；变更历史追加一行
  **覆盖 AC**: AC-44
  **依赖**: T046

---

## 实际偏差记录

> **只留一行指针**，论证在 design.md（决策 / 坑），这里不重复（见 `docs/SDD-Guide.md` §4）。
> 推翻已 ★ 确认的决策时，先停下与用户重新确认（§3 第四个 ★），再记录。

- **T006 与 T008/T015 的部分内容必须同一次落地**（2026-11-20）：`workflow:read` 的 `endpoints` 元组扩一条 + 客户契约 JSON 刷新，被两条**既有**测试夹住——`test_open_api_route_matrix.py::test_route_registry_matches_complete_key_authenticated_surface`（注册表与真实 v2 路由集双向相等）与 `test_openapi_schema_contract.py::test_generated_customer_contract_matches_registered_v2_http_routes`（契约端点集与 app.openapi() 完全相等）。只落 T006 本体会让这两条既有测试立刻红，因此把 T008 的 tuple 扩展与 T015 的契约刷新（仅新增 `/api/v2/flow/node_templates` 一块，其余漂移属其他 feature 不动）并入本次改动。T008/T015 剩余部分（`workflow:write` scope、后续端点的契约刷新）仍按原任务走
- **契约 JSON 只增量刷新**（2026-11-20）：`generate_openapi_contract.py` 重跑会连带把**已入库契约相对当前代码的既有漂移**（其他 feature 的描述/约束更新）一起写进去。本次用脚本只把新增 path 块合入已提交版本，既有内容一字不动——那些漂移归它们自己的 feature 提交
- **G8 第一轮审查修复（2026-11-20，44/50 无 Critical/High）**：① v2 端点从裸数组改为 `resp_200` 信封——design.md 手动验证用 `jq '.data | length'`，且既有 v2 读端点（`get_one_flow` → `UnifiedResponseModel`）都是信封形状，裸数组会让 CLI 与文档按 `.data` 取到 null；② 工具输出参数 `label` 发布的是 i18n 键 `output` 而非前端 `getToolTree` 原样的中文文案"输出变量"——这是 AC-03（不发界面文案）的**有意转换**，译文归前端 `node.tool.output`；③ 前端 `workflowTemplate` 字面量"文件在但解析不了"从静默跳过改为显式失败——常量被改名/移动时不能静默失去 live 对照
- **G8 第二轮审查修复（2026-11-20，45/50 无 Critical/High）**：① 契约 JSON 的 `description` 因信封修复而漂移——`resp_200` 说明进 docstring 后契约里还是旧一行，已重新同步该 path 块，并在 `test_openapi_schema_contract.py` 新增 `test_f005_node_templates_contract_block_is_exact`（该 path 整块与 `app.openapi()` 逐字段比对——既有契约测试只比 operation 集与 security，metadata 漂移不红）；② `flow_templates.py` 的 `resp_200` 改从 `bisheng.common.schemas.api` 导入（照 `llm.py`/`citation.py` 先例，静态端点不拉 v1 schema 模块的重依赖）；③ `node_templates.py` 顶部注释与实际矛盾（说"不加默认值"但 `value` 默认值是黄金快照的一部分）已改写；④ 缺 scope 测试改为逐键断言错误 data 只含 `required` 说明；⑤ marker 测试显式断言 `hosted_app is False`（钉住应用型主体默认拒绝）；⑥ tool 模板测试改名 `test_tool_node_template_contract_is_frozen` 语义与保障一致
- **T003 的"未登录 401"改为依赖装配断言**（2026-11-20）：`AuthJwt` 的密钥来自配置表，未认证请求在无数据库环境直接抛异常，状态码随环境变（401/422/500）。改为断言路由挂着 `UserPayload.get_login_user` 依赖 + 匿名调用拿不到模板数据（不论以何种方式失败）
