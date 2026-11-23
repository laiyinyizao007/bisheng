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
| 实现 | 🔄 进行中 | Wave 1–6 完成，T045 的浏览器手动验证待做（44 / 47）。偏差处理见 design.md 顶部调整原则 + `docs/SDD-Guide.md` §3-§4 |

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

- [x] **T007**: 【测试】scope 注册表测试
  **文件**: `src/backend/test/open_api/test_scopes.py`
  **逻辑**: 断言 ① `workflow:write` 在 `OPEN_API_SCOPES` 里存在，其 `group` 是 `"app"`（**不是** `local_dev_toolkit`）、`label_key` / `desc_key` 前缀是 `openApiManagement.scopes.`；② `workflow:read` 的 `endpoints` 元组已含四条新读端点；③ `workflow:write` 的 `endpoints` 元组覆盖六条写端点路径；④ 元组里的每条路径**都有实际注册的路由**（比对 `app.routes`，`delegate` / `identity:read` 除外——这两个本来就没有路由）；⑤ 装饰器 `@open_api_scope("workflow:write")` 能解析（不抛 `ValueError`）
  **测试**: `test_workflow_write_scope_registered` / `test_workflow_read_scope_extended` / `test_scope_endpoints_match_registered_routes` / `test_scope_group_is_not_local_dev_toolkit`
  **覆盖 AC**: AC-38
  **依赖**: 无

- [x] **T008**: 【实现】scope 注册
  **文件**: `src/backend/bisheng/open_api/domain/scopes.py`
  **逻辑**: ① **扩展**既有 `workflow:read` 的 `endpoints` 元组，追加 `("GET", f"{_V2}/flow/node_templates")`、`("GET", f"{_V2}/flows/{{flow_id}}/versions")`、`("GET", f"{_V2}/flows/{{flow_id}}/versions/{{version_id}}")`、`("GET", f"{_V2}/tools")`；② 新增 `OpenApiScope("workflow:write", …)`，覆盖 `POST /api/v2/flows`、`POST /api/v2/flows/{flow_id}/versions`、`PUT /api/v2/flows/{flow_id}/versions/{version_id}`、`POST /api/v2/flows/{flow_id}/versions/{version_id}/activate`、`POST /api/v2/tools`、`POST /api/v2/tools/schema`，`group="app"`。**必须与引用它的端点任务在同一提交里**（装饰器在 import 期校验）。既有 scope 条目**一行不改**（只加 `workflow:read` 的四条端点）
  **测试**: T007 全部通过
  **覆盖 AC**: AC-38, AC-44
  **依赖**: T007

- [x] **T009**: 【测试】v2 工作流端点的权限双层测试
  **文件**: `src/backend/test/open_api/test_workflow_write_v2.py`
  **逻辑**: 用 `test/conftest.py` 的 `test_client` + `mock_openfga` + `tenant_context` fixture。覆盖：<br>① 缺 `workflow:write` scope → 拒绝（不建版本）；<br>② 凭据**无资源归属人** → 拒绝（不得当成"对任何工作流都有权"）；<br>③ 归属人对该工作流**无 edit 权限** → 拒绝；<br>④ `hosted_app` 主体 → 拒绝；<br>⑤ 正常路径：建版本成功、版本 `data` 等于提交值、**既有版本的 `data` 未被改写**、切当前版本后 `Flow.data` 等于新版本 data；<br>⑥ 工作流处于**上线**状态时切当前版本 → 10525；<br>⑦ 响应里**没有任何"指定他人身份"的入参**（请求体多传身份字段被拒）；<br>⑧ 读端点（`GET /versions`、`GET /versions/{id}`）只需 `workflow:read`，无需 `workflow:write`
  **测试**: `test_missing_scope_refused` / `test_no_resource_owner_refused` / `test_non_owner_edit_refused` / `test_hosted_app_refused` / `test_create_version_does_not_touch_existing_versions` / `test_activate_updates_flow_data` / `test_activate_online_workflow_returns_10525` / `test_no_impersonation_entry_point` / `test_read_endpoints_need_only_read_scope`
  **覆盖 AC**: AC-25, AC-27, AC-28, AC-29, AC-39, AC-40, AC-42, AC-43
  **依赖**: T008

- [x] **T010**: 【实现】v2 工作流端点 + 路由挂载
  **文件**: `src/backend/bisheng/open_endpoints/api/endpoints/workflow_write.py`（新）、`src/backend/bisheng/api/router.py`
  **逻辑**: router（prefix `/flows`）。端点：<br>**读**（`@open_api_scope("workflow:read")`）：`GET /{flow_id}/versions`、`GET /{flow_id}/versions/{version_id}`；<br>**写**（`@open_api_scope("workflow:write", modes=("S",))`，**仅服务账号模式**，不给"代他人写"的入口）：`POST /`、`POST /{flow_id}/versions`、`PUT /{flow_id}/versions/{version_id}`、`POST /{flow_id}/versions/{version_id}/activate`。<br>身份从 `get_current_open_api_principal()` 读，操作者用 `await get_open_api_operator_async()` 取（服务账号拿到的 `UserPayload` 角色为空、非管理员）。**业务逻辑一律委托既有 `WorkFlowService`（`api/services/flow.py`）**：`judge_flow_write_permission` / `create_new_version` / `change_current_version` / `update_version_info`。**不新写授权逻辑、不 import `database.models`、不新加 DAO 入口**
  **测试**: T009 全部通过
  **覆盖 AC**: AC-25, AC-26, AC-27, AC-28, AC-29, AC-39, AC-40, AC-42, AC-43
  **依赖**: T009

- [x] **T011**: 【测试】v2 工具端点测试
  **文件**: `src/backend/test/open_api/test_tools_v2.py`
  **逻辑**: 覆盖：`POST /api/v2/tools/schema` 把 OpenAPI 描述解析成工具类型（每个 operation 成为一个 child，`tool_key` 非空）；`POST /api/v2/tools` 注册成功；`GET /api/v2/tools` 列出（只需 `workflow:read`）；**缺 `workflow:write` scope 时注册被拒**（不返回成功）；**缺 operationId 时注册失败并说明缺哪一项**（operationId 是工作流引用工具的唯一依据）
  **测试**: `test_schema_parses_operations` / `test_register_creates_tool_type` / `test_list_returns_registered_tools` / `test_missing_scope_refused` / `test_missing_operation_id_refused` / `test_list_needs_only_read_scope`
  **覆盖 AC**: AC-34, AC-35, AC-36, AC-37
  **依赖**: T008

- [x] **T012**: 【实现】v2 工具端点 + 路由挂载
  **文件**: `src/backend/bisheng/open_endpoints/api/endpoints/tools_v2.py`（新）、`src/backend/bisheng/api/router.py`
  **逻辑**: router（prefix `/tools`）。`GET /`（列出，挂 `workflow:read`）、`POST /`（注册，挂 `workflow:write, modes=("S",)`）、`POST /schema`（解析 OpenAPI，挂 `workflow:write, modes=("S",)`）。**复用既有 `ToolServices`（`bisheng.tool.domain.services.tool`）的 `parse_openapi_schema` / `add_tools`**，不 import `bisheng.tool.api.tool`（那会命中 arch-guard RULE-5 的跨模块 API 导入）
  **测试**: T011 全部通过
  **覆盖 AC**: AC-34, AC-35, AC-36, AC-37
  **依赖**: T011

- [x] **T013**: 【测试】工具列表的 `node_template` 字段测试
  **文件**: `src/backend/test/workflow/test_tool_node_template.py`
  **逻辑**: 断言 `GET /api/v1/tool` 的每个 child **新增**了 `node_template` 字段，且它等于 `get_tool_node_template(child)` 的产出；**既有字段一字未变**（`name` / `desc` / `api_params` / `tool_key` 仍在原位）
  **测试**: `test_each_child_carries_node_template` / `test_existing_child_fields_unchanged` / `test_node_template_matches_generator`
  **覆盖 AC**: AC-04
  **依赖**: T002

- [x] **T014**: 【实现】工具列表的 `node_template` 字段
  **文件**: `src/backend/bisheng/tool/domain/services/tool.py`
  **逻辑**: 在 `get_tool_list` 组装 children 的地方，**附加**（不改、不删）`node_template=get_tool_node_template(child)`。纯增量字段
  **测试**: T013 全部通过
  **覆盖 AC**: AC-04
  **依赖**: T013

- [x] **T015**: 【实现】刷新开放 API 客户契约
  **文件**: `features/v3.0.0-beta1/053-openapi-auth-and-identity/openapi-v2-key-auth-api.json`、同目录的 `openapi-v2-key-auth-api.md` 与 `openapi-v2-key-auth-api-by-category.md`
  **逻辑**: 跑 `python features/v3.0.0-beta1/053-openapi-auth-and-identity/generate_openapi_contract.py` 重新生成 JSON（该脚本会强制打开 `open_platform.enabled` 再 import app，因此产出的是完整面）。再按新端点补齐两份人读 md 里按 scope 罗列端点的部分。**这是新增 v2 端点的必要配套**——`test/open_api/test_openapi_schema_contract.py::test_generated_customer_contract_matches_registered_v2_http_routes` 断言 v2 路由集与契约文档端点集**完全相等**，不刷新这条既有测试立刻红
  **测试**: `cd src/backend && uv run pytest test/open_api/test_openapi_schema_contract.py -v` 全部通过
  **覆盖 AC**: AC-01, AC-25, AC-27, AC-29, AC-34, AC-36
  **依赖**: T010, T012

### Wave 3 — CLI：声明加载、覆盖、编译、校验

> 这一波把编译器建起来（design 决策 1、6、7）。CLI 侧**零新增依赖**，YAML 用 `SafeLoader` 子类。

- [x] **T016**: 【测试】CLI 命令族契约测试
  **文件**: `src/bisheng-cli/tests/test_cli.py`
  **逻辑**: 参数化 over 全部已注册命令的每个叶子：① 全局 flag（`--json` / `--verbose` / `--quiet` / `--timeout` / `--no-proxy` / `--platform`）都镜像到了叶子；② `bisheng --json <cmd>` 与 `bisheng <cmd> --json` 两种顺序解析出**同一个** `json_mode`；③ `SUBCOMMANDS` 里没有桩；④ 每条退出路径恰好发出一个 `result` 事件
  **测试**: `test_every_leaf_mirrors_global_flags` / `test_flag_order_is_equivalent` / `test_no_subcommand_is_a_stub` / `test_exactly_one_result_event`
  **覆盖 AC**: AC-23
  **依赖**: 无

- [x] **T017**: 【实现】CLI `flow` 命令族接入（分发骨架）——**挪到 Wave 5 与 T031 同批落地**（见实际偏差记录）
  **文件**: `src/bisheng-cli/bisheng_cli/cli.py`、`src/bisheng-cli/bisheng_cli/main.py`、`src/bisheng-cli/bisheng_cli/commands/flow.py`（新）
  **逻辑**: `cli.py` 的 `SUBCOMMANDS` 加 `"flow"`；`flow` 是**两 token 命令**（照 `skills` / `platforms` 的形状）：`flow` 自己的 subparser 携带动词，每个动词叶子再镜像一遍全局 flag（`default=argparse.SUPPRESS`）；`main.py` 的 `_HANDLER_PATHS` 加 `"flow": "bisheng_cli.commands.flow:run"`；`commands/flow.py` 实现分发。**此时只注册已实现的子命令**（上游铁律：命令要么注册要么不存在，绝不注册桩）——本任务只接通 `compile` 的分发路径，其余动词在后续任务里追加。三处必须同时改，否则命令注册不完整
  **测试**: T016 全部通过
  **覆盖 AC**: AC-23
  **依赖**: T016

- [x] **T018**: 【测试】YAML 加载层测试
  **文件**: `src/bisheng-cli/tests/test_flow_spec.py`
  **逻辑**: 覆盖：① 含 `!!python/object` 标签的声明文件**被拒**（照 `project.py` 的 `yaml.safe_load` 做法，不得执行）；② `!include` 展开为文件内容（路径相对**声明文件所在目录**，不是 CWD）；③ 缺失必填顶层字段 → 本地拒绝并指出缺哪一项，**且不发起任何平台请求**（`no_network` 哨兵会抓住）；④ 顶层不是键值映射 → 拒绝；⑤ `!include` 指向缺失文件 → 报错并给出解析后的路径
  **测试**: `test_python_object_tag_is_rejected` / `test_include_expands_relative_to_spec_dir` / `test_missing_top_level_field_rejected_without_request` / `test_non_mapping_top_level_rejected` / `test_missing_include_file_reports_path`
  **覆盖 AC**: AC-07, AC-10, AC-11
  **依赖**: T017

- [x] **T019**: 【实现】YAML 加载层
  **文件**: `src/bisheng-cli/bisheng_cli/flow/spec.py`
  **逻辑**: 定义 `SafeLoader` 子类并注册**唯一**一个自定义构造器 `!include`（只读文本文件、返回内容；**不 eval、不查类、不导入模块**）；`load_spec(path) -> dict` 做本地快检：可安全加载、顶层是映射、必填顶层字段齐全。**不填默认值、不裁定节点类型合法性**（那是服务端的事）
  **测试**: T018 全部通过
  **覆盖 AC**: AC-07, AC-10, AC-11
  **依赖**: T018

- [x] **T020**: 【测试】受限覆盖层测试
  **文件**: `src/bisheng-cli/tests/test_flow_overlay.py`
  **逻辑**: 允许：覆盖 YAML 已声明节点的**已声明参数**取值、追加 YAML 未声明的**节点**、追加 YAML 未声明的**连线**。拒绝（并**逐项列出被拒项**：哪条声明的哪个参数、哪条已声明的节点/连线）：删除节点、删除连线、改一条已声明连线的 `sourceHandle` / `target`、改已声明节点的 `id` / `type`、新增 YAML 未声明的参数键
  **测试**: `test_can_override_declared_param` / `test_can_append_undeclared_node` / `test_can_append_undeclared_edge` / `test_delete_node_refused_and_itemised` / `test_delete_edge_refused_and_itemised` / `test_change_edge_handle_refused` / `test_change_node_id_refused` / `test_new_undeclared_param_refused`
  **覆盖 AC**: AC-08, AC-09
  **依赖**: T019

- [x] **T021**: 【实现】受限覆盖层
  **文件**: `src/bisheng-cli/bisheng_cli/flow/overlay.py`
  **逻辑**: `apply_overlay(spec, overlay_module) -> dict`。覆盖层是一个 Python 文件，导出 `apply(spec)`；执行它（用户显式指定 `--overlay` 时执行自己的代码——这是**唯一会被执行的代码**，YAML 永远不执行）。应用结果按 T020 的语义表校验，违规**逐项报错**。**限制的是它对工作流结构的影响力，不是它的代码能力**（文档必须说清这一点）
  **测试**: T020 全部通过
  **覆盖 AC**: AC-08, AC-09
  **依赖**: T020

- [x] **T022**: 【测试】编译器测试
  **文件**: `src/bisheng-cli/tests/test_flow_compiler.py`
  **逻辑**: 覆盖：各节点类型产出**完整**的 node 骨架（`id` / `type:"flowNode"` / `position` / `data`，`data` 里的 `group_params` 结构来自取到的模板）；`params` 里声明的键被填进对应分组对应参数；工具节点按 `tool: <operationId>` 解析出 `tool_key` 并生成工具模板骨架；**工具引用解析不到时报错**（不产出缺 `tool_key` 的节点）；`!include` 展开的提示词进入 `system_prompt` / `user_prompt`；产出 `{nodes, edges, viewport}` 三部分
  **测试**: `test_each_node_type_produces_full_skeleton` / `test_params_filled_into_matching_group` / `test_tool_node_resolves_tool_key` / `test_unknown_operation_id_is_error` / `test_included_prompt_expanded` / `test_output_has_nodes_edges_viewport`
  **覆盖 AC**: AC-06
  **依赖**: T019, T021, T014

- [x] **T023**: 【实现】编译器
  **文件**: `src/bisheng-cli/bisheng_cli/flow/compiler.py`
  **逻辑**: `compile_spec(spec, templates, tools) -> dict`。流程：按 `type` 深拷贝节点模板骨架 → 把声明的 `params` 填进匹配分组的匹配参数（**按 key 匹配，不按位置**）→ 工具节点用后端提供的工具模板（由 v2 工具端点的 `node_template` 字段带回）→ 生成 edges（`id` / `source` / `sourceHandle` / `target` / `targetHandle` / `animated` / `type`）→ 产出 `{nodes, edges, viewport}`
  **测试**: T022 全部通过
  **覆盖 AC**: AC-06
  **依赖**: T022

- [x] **T024**: 【测试】编译期结构校验测试（双向用例）
  **文件**: `src/bisheng-cli/tests/test_flow_validate.py`
  **逻辑**: **每条规则两条用例**——"该报的报出来" + "不该报的不误报"（只写正例的测试等于没测）。12 条规则：① 缺节点 ② 缺 start ③ 节点 id 空 / 重复 ④ 未知节点类型 ⑤ 非 end 节点无连线 ⑥ 边指向不存在的节点 ⑦ `sourceHandle` 不是该节点的合法出口（condition 的 case id 或 `right_handle`）⑧ 变量引用格式错 / 引用到的节点不存在 ⑨ 缺该节点类型的必填参数（**按 design §4.4 的必填参数表逐类型查**，`output` 节点的 `output_msg`/`message` 二选一、`agent` 的 `sql_agent` 条件必填）⑩ `batch_variable` 与 `output[i].key` 不一一对应 ⑪ `tool_key` 解析不到 ⑫ `condition` 数组 / case / 条件的必填项缺失。每条报错必须**可定位**（哪个节点、哪个参数、哪个引用）
  **测试**: 每个规则一个 `test_reports_<rule>` + 一个 `test_accepts_valid_<rule>`
  **覆盖 AC**: AC-12, AC-13, AC-14, AC-15, AC-16, AC-17, AC-18
  **依赖**: T023

- [x] **T025**: 【实现】编译期结构校验
  **文件**: `src/bisheng-cli/bisheng_cli/flow/validate.py`
  **逻辑**: `validate(compiled) -> list[Issue]`，12 条规则按 design §4.4 实现；每条 Issue 带可定位信息（节点 id / 参数键 / 引用 / 连线两端与句柄）。**只做"确定无疑"的检查**——不判断"某参数在平台上是否参与执行"，不判断"某资源 id 是否还有效"（design §5 坑 21）
  **测试**: T024 全部通过
  **覆盖 AC**: AC-12, AC-13, AC-14, AC-15, AC-16, AC-17, AC-18
  **依赖**: T024

### Wave 4 — CLI：客户端层与 diff

- [x] **T026**: 【测试】CLI 客户端层测试
  **文件**: `src/bisheng-cli/tests/test_flow_client.py`
  **逻辑**: 覆盖：模板取回与**缓存**（命中缓存不发请求、缓存失效时重取、**取不到时失败且不回落任何硬编码副本**）；工具列表取回与 `operationId → tool_key` 解析；版本读取（默认基准 = 当前版本、指定版本 = 历史版本）；**线上不存在 / 无权读取 / 读取失败三种情形分别给出可行动的原因**（不报"无差异"）；平台错误码 → `CliError` 的映射（`details` / `hints` 原样透传）；**输出里不出现凭据明文**
  **测试**: `test_templates_cached_and_invalidated` / `test_template_fetch_failure_has_no_fallback` / `test_operation_id_resolves_to_tool_key` / `test_missing_workflow_distinguished_from_forbidden` / `test_platform_error_becomes_clierror` / `test_no_credential_in_output`
  **覆盖 AC**: AC-05, AC-24, AC-28, AC-41
  **依赖**: T006, T012, T014

- [x] **T027**: 【实现】CLI 客户端层
  **文件**: `src/bisheng-cli/bisheng_cli/flow/client.py`
  **逻辑**: 用既有 `PlatformClient`（`bisheng_cli/http.py`）封装 v2 调用：取节点模板（带按 `base_url` 的本地缓存，缓存是**性能优化不是副本**，取不到就失败）、取工具列表、读版本、建版本、切当前版本、注册工具、invoke。平台错误码经 `errors.py` 翻成 `CliError`（`as_failure()` 的五元组）。**不做权限判定**
  **测试**: T026 全部通过
  **覆盖 AC**: AC-05, AC-24, AC-28, AC-41
  **依赖**: T026

- [x] **T028**: 【测试】diff 算法测试（双向探针）
  **文件**: `src/bisheng-cli/tests/test_flow_diff.py`
  **逻辑**: **必须报出来**：线上独有节点、本地独有节点、同一节点上取值不同的参数、线上独有连线、本地独有连线。**必须不算差异**：仅 `position` 不同、仅 `viewport` 不同、仅 `data.v` 不同、仅参数的 `label` / `desc` / `type` / `required` 不同。**必须算差异**：`tab.value` 不同、`tool_key` 不同、`name` 不同。参数比较**按 key 不按位置**（加一个参数不应让其余参数全被报成"变了"）。差异项必须可定位（节点 id / 参数键 / 连线两端与句柄）
  **测试**: `test_reports_node_only_online` / `test_reports_node_only_local` / `test_reports_changed_param` / `test_reports_edge_only_online` / `test_reports_edge_only_local` / `test_position_only_is_not_a_diff` / `test_viewport_only_is_not_a_diff` / `test_node_version_only_is_not_a_diff` / `test_param_label_only_is_not_a_diff` / `test_tab_value_is_a_diff` / `test_tool_key_is_a_diff` / `test_params_compared_by_key_not_position`
  **覆盖 AC**: AC-19, AC-20, AC-21, AC-22, AC-23
  **依赖**: T023

- [x] **T029**: 【实现】diff 算法
  **文件**: `src/bisheng-cli/bisheng_cli/flow/diff.py`
  **逻辑**: `diff(local, online) -> dict`，按 design 决策 5 的比较规则表实现：节点按 `id` 索引；参数按 `group_params[].params[].key` 展平比 `value`；节点其他字段比 `name` / `description` / `tab` / `tool_key`；连线按 `(source, sourceHandle, target)`；排除 `position` / `viewport` / `data.v` / 参数元数据。输出机器可读且**人读呈现与机器可读呈现内容一致**
  **测试**: T028 全部通过
  **覆盖 AC**: AC-19, AC-20, AC-21, AC-22, AC-23
  **依赖**: T028

### Wave 5 — CLI：命令接线（compile / diff / push / run / tool）

> 每个任务**追加**一个子命令分支到 `commands/flow.py`，不重写已存在的分支。

- [x] **T030**: 【测试】`flow compile` 命令测试
  **文件**: `src/bisheng-cli/tests/test_flow_command_compile.py`
  **逻辑**: 覆盖：合法声明 → 输出 `{nodes, edges, viewport}`；`--json` 时 NDJSON 走 stdout、人读文本走 stderr、**最后一行恒为 `result` 事件**；校验失败 → 退出码与可读原因（**不是堆栈**），`details` 里带全部问题项；`--overlay` 生效；`--out FILE` 写出结果
  **测试**: `test_compile_emits_definition` / `test_json_mode_splits_streams` / `test_result_event_is_last_line` / `test_validation_failure_reports_all_issues` / `test_overlay_applied` / `test_out_file_written`
  **覆盖 AC**: AC-06, AC-09, AC-18, AC-23
  **依赖**: T025, T027

- [x] **T031**: 【实现】`flow compile` 命令
  **文件**: `src/bisheng-cli/bisheng_cli/commands/flow.py`
  **逻辑**: `compile` 分支：加载声明 → 应用覆盖层 → 取模板与工具列表 → 编译 → 校验 → 输出（`--json` 走 NDJSON；人读文本走 stderr）。**校验失败时退出码非 0，`details` 带全部问题项**
  **测试**: T030 全部通过
  **覆盖 AC**: AC-06, AC-09, AC-18, AC-23
  **依赖**: T030

- [x] **T032**: 【测试】`flow diff` 命令测试
  **文件**: `src/bisheng-cli/tests/test_flow_command_diff.py`
  **逻辑**: 覆盖：默认基准 = 线上当前版本；`--version-id` 指定历史版本为基准；`--list-versions` 列出可用版本；差异输出**机器可读**（`details` 里的节点/参数/连线差异项可定位）；**人读呈现与机器可读呈现的差异内容一致**；线上不存在 / 无权 / 失败三态分别报错（**不报"无差异"**）；工作流 id 绑定写在项目内 `.bisheng/flow.json`（形状照 `.bisheng/app.json`）
  **测试**: `test_defaults_to_current_version` / `test_version_id_selects_historical_baseline` / `test_list_versions` / `test_machine_readable_details` / `test_human_and_machine_agree` / `test_three_failure_modes_distinguished` / `test_flow_binding_written_to_bisheng_dir`
  **覆盖 AC**: AC-19, AC-20, AC-21, AC-22, AC-23, AC-24
  **依赖**: T029, T031

- [x] **T033**: 【实现】`flow diff` 命令
  **文件**: `src/bisheng-cli/bisheng_cli/commands/flow.py`
  **逻辑**: `diff` 分支：编译本地 → 取线上基准 → 跑 diff → 输出（结构化 + 人读）。`--version-id` / `--list-versions` 走 `GET /api/v2/flows/{flow_id}/versions[/{version_id}]`。**不做权限判定**（服务端说了算）
  **测试**: T032 全部通过
  **覆盖 AC**: AC-19, AC-20, AC-21, AC-22, AC-23, AC-24
  **依赖**: T032

- [x] **T034**: 【测试】`flow push` 命令测试
  **文件**: `src/bisheng-cli/tests/test_flow_command_push.py`
  **逻辑**: 覆盖：<br>① 推送后**新建**了一个版本，**既有版本的 data 未被改写**；<br>② 推送后**切为当前版本**，`Flow.data` 等于新版本 data；<br>③ 线上有独有内容时：**无 TTY → 拒绝**（不默认同意），要求 `--allow-drop-online-only`；有 TTY → 展示被保留在旧版本里的内容并要求确认；<br>④ 上线状态 → 报出"版本已建，但工作流在线上，需先下线"，**且不报推送成功**；<br>⑤ 权限不足 → 明确报出**缺少哪个权限位**，不退化为只读、不静默跳过；<br>⑥ 成功 → 报出**新版本的标识与版本名**；<br>⑦ `--no-activate` 只建版本不切
  **测试**: `test_push_creates_new_version` / `test_push_activates_new_version` / `test_existing_versions_untouched` / `test_online_only_content_refused_without_tty` / `test_online_only_content_requires_confirmation_with_tty` / `test_online_workflow_reports_offline_hint` / `test_missing_permission_reports_which_scope` / `test_reports_new_version_id_and_name` / `test_no_activate_only_creates_version`
  **覆盖 AC**: AC-25, AC-26, AC-27, AC-28, AC-29
  **依赖**: T033

- [x] **T035**: 【实现】`flow push` 命令
  **文件**: `src/bisheng-cli/bisheng_cli/commands/flow.py`
  **逻辑**: `push` 分支：编译 → 取当前版本 → diff → 有线上独有内容时按 `cli.py` 的 `confirm()` 语义处理（**无 TTY 时抛 `CliError(EXIT_USAGE)` 要求显式 flag，不默认同意**）→ `POST /api/v2/flows/{flow_id}/versions` 建新版本 → `POST .../activate` 切当前版本 → 报版本 id 与名称。`--no-activate` 只建不切；`--create` 先 `POST /api/v2/flows` 建工作流
  **测试**: T034 全部通过
  **覆盖 AC**: AC-25, AC-26, AC-27, AC-28, AC-29
  **依赖**: T034

- [x] **T036**: 【测试】`flow run` 命令测试
  **文件**: `src/bisheng-cli/tests/test_flow_command_run.py`
  **逻辑**: 覆盖：事件流被呈现出来；停在等待输入的节点时**报出停在哪个节点与等待什么输入**；续跑时**同时带 `session_id` 与 `message_id`**（缺一不可 —— 有专门的用例断言两个都发出去了）；节点失败时**报出失败节点与失败原因**（不只报"运行结束"）；**无 TTY 时不阻塞等待人工输入**（`--input` / `--input-file` 供一次性输入，跑到下一次等待输入就结束）
  **测试**: `test_events_are_streamed` / `test_reports_paused_node_and_expected_input` / `test_continuation_sends_session_and_message_id` / `test_node_failure_reports_node_and_reason` / `test_no_tty_does_not_block`
  **覆盖 AC**: AC-30, AC-31, AC-32, AC-33
  **依赖**: T027

- [x] **T037**: 【实现】`flow run` 命令
  **文件**: `src/bisheng-cli/bisheng_cli/commands/flow.py`
  **逻辑**: `run` 分支：调 `POST /api/v2/workflow/invoke`（`workflow:invoke` scope，既有端点），呈现事件流；遇 input 事件时报出节点与待输入内容，交互式读 stdin / 非交互式用 `--input` / `--input-file`；续跑时 body **同时带 `session_id` 与 `message_id`**
  **测试**: T036 全部通过
  **覆盖 AC**: AC-30, AC-31, AC-32, AC-33
  **依赖**: T036

- [x] **T038**: 【测试】`flow tool` 命令测试
  **文件**: `src/bisheng-cli/tests/test_flow_command_tool.py`
  **逻辑**: 覆盖：注册后**报出工具类型标识与各可用操作的标识**（工作流声明要按 operationId 引用它们）；`list` 报出各工具类型及其可用操作；权限不足时**明确报出权限不足**（**不报注册成功**）
  **测试**: `test_register_reports_tool_and_operation_ids` / `test_list_reports_types_and_operations` / `test_permission_error_not_reported_as_success`
  **覆盖 AC**: AC-34, AC-35, AC-36, AC-37
  **依赖**: T027

- [x] **T039**: 【实现】`flow tool` 命令
  **文件**: `src/bisheng-cli/bisheng_cli/commands/flow.py`
  **逻辑**: `tool` 分支的两个动词：`register <openapi.yaml>`（调 `POST /api/v2/tools/schema` → `POST /api/v2/tools`，报出工具类型标识与各操作标识）与 `list`（调 `GET /api/v2/tools`，报出各工具类型及其可用操作）
  **测试**: T038 全部通过
  **覆盖 AC**: AC-34, AC-35, AC-36, AC-37
  **依赖**: T038

- [x] **T040**: 【测试】105xx 错误码 → 退出码映射测试
  **文件**: `src/bisheng-cli/tests/test_errors.py`
  **逻辑**: 断言 `ERROR_EXIT_CODES` 覆盖 10525 / 10529 / 10502 / 10500 / 10543 / 10599 并映射到预期的退出码（`EXIT_PRECHECK_FAILED` / `EXIT_PUBLISH_CONFLICT` / `EXIT_LOCAL_INVALID` / `EXIT_FORBIDDEN`）；断言缺 scope（26003）的既有中文提示仍生效；断言**新增条目没有改动任何既有映射**（别把别的命令的退出码改掉）
  **测试**: `test_flow_error_codes_mapped` / `test_missing_scope_hint_preserved` / `test_existing_mappings_unchanged`
  **覆盖 AC**: AC-28, AC-41
  **依赖**: 无

- [x] **T041**: 【实现】105xx 错误码 → 退出码映射
  **文件**: `src/bisheng-cli/bisheng_cli/errors.py`
  **逻辑**: 在 `ERROR_EXIT_CODES` **新增** 105 段映射（10525 → `EXIT_PRECHECK_FAILED`；10529 / 10502 → `EXIT_PUBLISH_CONFLICT`；10500 / 10543 → `EXIT_LOCAL_INVALID`；10599 → `EXIT_FORBIDDEN`），并在 `ERROR_HINTS` 补中文说明（**只增不改**既有条目）
  **测试**: T040 全部通过
  **覆盖 AC**: AC-28, AC-41
  **依赖**: T040

### Wave 6 — 前端 Platform

- [x] **T042**: 【实现】前端节点模板改从接口取
  **文件**: `src/frontend/platform/src/controllers/API/workflow.ts`
  **逻辑**: `getWorkflowNodeTemplate()` 改为 `axios.get('/api/v1/workflow/node_templates')`（走既有 `controllers/API/request` 封装，**不在 store 里直接调 HTTP**）；**删除**文件里那份约 890 行的 `workflowTemplate` 字面量。改前先用 `grep -rn "workflowTemplate" src/` 全量确认没有别的引用。**前提**：T001 已把黄金数据固化进测试文件（否则 T001 的比对会因前端字面量被删而失效）
  **覆盖 AC**: AC-01, AC-02
  **手动验证**:
  - 打开工作流画布（`/build`），节点库侧栏应列出 12 类节点，拖拽可用
  - 打开知识库问答助手的快捷建流，模板能正常取到
  - 后端接口返回 500 时，节点库侧栏给出可读原因（**不回落硬编码副本**）
  **依赖**: T004

- [x] **T043**: 【实现】前端 `getToolTree` 收口到后端
  **文件**: `src/frontend/platform/src/util/flowUtils.ts`
  **逻辑**: `getToolTree` 的**形状拼装逻辑全部删除**，改为 `children.map(c => c.node_template)`；**返回形状不变**（仍是 `{name, is_preset, children}`），前端 `Sidebar.tsx` 的消费方式不改
  **覆盖 AC**: AC-04
  **手动验证**:
  - 工作流画布的"工具"页签，展开任一自定义工具 → 参数区与输出区与改造前一致
  - 拖一个工具节点进画布 → 节点参数区完整
  **依赖**: T014, T042

- [x] **T044**: 【实现】新 scope 的界面文案（三语言）
  **文件**: `src/frontend/platform/public/locales/zh-Hans/bs.json`、`src/frontend/platform/public/locales/en-US/bs.json`、`src/frontend/platform/public/locales/ja/bs.json`
  **逻辑**: 在 `openApiManagement.scopes` 下新增 `workflow_write` 的 `label` 与 `desc`（键名与 `scopes.py` 里 `label_key` / `desc_key` 一致：`openApiManagement.scopes.workflow_write.label` / `.desc`）。**三个语言必须同时加**——`pnpm check-i18n` 校验键集对齐（参照语言 zh-Hans）。既有条目不动
  **覆盖 AC**: AC-38
  **手动验证**:
  - 服务账号签发表单里出现"工作流写入"这一项，中/英/日三种语言都显示人话（不是裸键名）
  **依赖**: T008

- [ ] **T045**: 前端回归确认（自动化部分已完成：lint / typecheck / check-i18n 通过、vitest 无新增失败；**浏览器手动验证待重建本机镜像后做**）
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
- **Wave 2 偏差（2026-11-21）**：① **创建工作流的编排抽成 `FlowService.create_workflow`**（T010 文件清单外改了 `api/services/flow.py` 与 `api/v1/workflow.py`）——v1/v2 两个写面共用一份实现，v1 端点只剩委托；"谁能建"（菜单门槛 + `WORKFLOW` 配额）留在各自端点，"建什么"在服务里。不这样做 v2 只能复制 v1 端点体，两份重名检查/租户赋值会漂移；② **v2 创建端点 import 了 `database.models`（`FlowCreate`）与 `WebMenuResource`**——arch-guard RULE-3 WARNING（迁移期、只告警），与 v1 `workflow.py` 同一形状；T010 写的"不 import `database.models`"对创建端点做不到（`FlowService.create_workflow` 的入参就是 `FlowCreate`）。import 在模块级，所以 arch-guard 的告警落在整个 `workflow_write.py` 上——版本端点自身不用 `database.models`，但同文件，不能说"版本端点守住了"；③ **`GET /api/v2/tools` 传 `is_preset=None`**（全部可用工具，含预置），不是只取自定义——编译器要能解析任意 operationId，预置工具也是合法引用；④ **T011 的 `test_missing_operation_id_refused` 换成 `test_service_error_codes_propagate`**——`GptsToolsTypeRead.name` 有默认值 `""`，"缺名字"不是请求校验层的事，端点层能如实守住的契约是"服务层 105xx/150xx 业务错误原样透出"；⑤ **工具清单用独立读模型**：子项 `GptsToolsWithNodeTemplate(GptsToolsRead)`（补齐表模型上的 `extra` + 声明 `node_template`——FastAPI 只序列化声明字段），类型层 `GptsToolsTypeListItem(GptsToolsTypeRead)` 只给 `get_tool_list` 用。**写模型 `GptsToolsTypeRead.children` 保持 `list[GptsTools]`**——它同时是 v1/v2 建工具的请求体，DAO 直接 `session.add_all` 子项（见 G8 R1 C1）；⑥ **契约刷新**：主契约 JSON 照上一条只增量合入 6 个新 path + 本 feature 拥有的 schema 传递闭包（最终 5 个：`OpenFlowCreate` / `OpenFlowVersionCreate` / `OpenToolSchemaParseRequest` / `OpenToolTypeCreate` / `OpenToolCreate`；中途一版漏了间接引用的 schema，被 `referenced <= schemas` 断言抓到）；`generate_openapi_contract.py` **直接覆写目标文件**，只能先跑、再从 HEAD 基线重做合并。分类版 JSON（人工策展：中文 tags/summary + `x-open-api-scope`）也只增量加入本 feature 的 10 个 operation 与新"工具"tag，不顺带补其他 feature 欠的端点。git 默认 Myers 对这份 JSON 会报出大量假删除，`--diff-algorithm=histogram` 显示为纯插入——看 diff 用 histogram；⑦ **主 md 头部"HTTP 接口：N 条"在 HEAD 就已失准**（声明 46、实际章节 42、契约 53），本次只按新增章节 +10 记为 56，既有漂移不在本 feature 修
- **Wave 2 G8 第一轮（2026-11-21，codex，38/50）**：C1（Critical）把 `GptsToolsTypeRead.children` 改成非表读模型 → v1 `POST/PUT /api/v1/tool` 与 v2 注册全部失败（`session.add_all` 要映射类；端点测试打桩了 `add_tools` 所以没抓到）——已恢复写模型、另建只读清单模型，补真实 DAO 落库用例 `test_insert_tool_type_persists_children`；M2 `data` 缺 nodes/edges 在端点体内才校验 → 500——改在请求 schema 上委托 v1 模型的校验器，**版本创建/更新同病一并修**（复核时发现，审查只点了创建）；M3 路径 `flow_id` 被丢弃，`PUT /flows/A/versions/<B的版本>` 静默改 B——`get_version_info` / `update_version_info` 加可选 `expected_flow_id`，写前拒（10500，与 activate 同语义），v1 不传、行为不变；M4 v2 注册沿用 v1 宽松读模型当请求体，`is_shared` 与子项 `tenant_id` 可由调用方设——改窄 schema `OpenToolTypeCreate` / `OpenToolCreate`（`extra=forbid`），默认值不复制、`exclude_unset` 交给 v1 模型。L5（清单每子项算模板）与 L6（`create_workflow` 里的同步会话，v1 原样搬来）接受不改
- **Wave 2 G8 第二轮（2026-11-21，codex 深度，38/50，确认 R1 四项修复正确）**：① `GET /api/v2/tools` 回 `api_key` 明文（v1 既有，但 v2 让只有 `workflow:read` 的机器钥匙也能读）——v2 清单摘掉 `api_key`，v1 浏览器面不动；② `POST /api/v2/tools/schema` 的 `download_url` 走共享的裸 `aiohttp.get`（无 scheme / 内网段 / 超时防护）= 机器钥匙可触发的 SSRF——v2 契约**去掉 `download_url`**，只收 `file_content`（CLI 本来就读本地文件），共享 helper 不动；③ `api_key` 超 1000 在端点体内校验 → 500 且错误里带部分密钥——请求 schema 加 `max_length=1000`；④ `is_preset` 未约束：`2`（MCP）会让服务端主动连调用方给的地址、`7` 会让 DSH 目录 KeyError——v2 只收 `0`（自定义 OpenAPI 工具）；⑤ 子工具 `name`/`logo` 无上限（列宽 125/512）——加 `max_length`，附"恰好在上限"的对照用例；⑥ RULE-3 告警覆盖整个 `workflow_write.py`——只改偏差记录措辞（见上 ②），不为消告警搬代码；⑦ 并发同名版本可重复（无唯一索引）、PUT 可复活软删版本——v1 既有行为且仍需 FGA edit，不在本 feature 修。**顺带自查出的错误**：文档与测试把 `is_preset=1` 当成 API，实际 `ToolPresetType.API = 0`、`1 = PRESET`（会被拒绝）——已更正
- **Wave 3 偏差（2026-11-21）**：① **T017 挪到 Wave 5，与 T031 同批**——T017 要求"只接通 `compile`"，但 `compile` 的实现在 T031，且依赖 Wave 4 的客户端层取模板；Wave 3 注册 `flow compile` 就是桩命令，违反上游"命令要么注册要么不存在"，T016 的 `test_no_subcommand_is_a_stub` 本身也会拦。T016 的契约测试是**对解析器参数化**的，`flow` 一注册就自动纳入，不用改测试；② **§4.4 按引擎修正四处**（论证见 design 修订历史 2026-11-21）：#5 进出边均可、#7 只判条件节点、#8 孤立 `{{#` 是正文不是引用、#12 case 的 `operator`/`conditions` 可选，另 `agent.sql_agent` 不列必填——按原表会误报合法定义（AC-18）；③ **节点类型清单不在 CLI 硬编码**：`validate(definition, templates, tools)` 从模板推出合法类型（决策 2），必填参数表与比较符集合按设计是 CLI 侧清单；④ **DSL 补键**（design §6.5 已同步）：节点 `name`/`description`/`position`，条件 `id`/`ref`/`label`，工具 `<类型名>/<operationId>` 限定名；未知键一律编译报错；⑤ **CLI 测试夹具 `tests/fixtures/flow/node_templates.json`** 是后端 `NODE_TEMPLATES` 的冻结副本（CLI 零耦合，测不到后端常量），由后端 `test_cli_fixture_matches_backend_templates` 钉在真值上；夹具只给测试用，运行时不回落（AC-05）；⑥ **验证**：T016 对现有 7 个命令参数化全绿 + 变异探针（拿掉一个叶子的镜像 → 正好那个叶子红）；T018 的"不执行"用例先证明载荷在 `unsafe_load` 下真的会执行（初版载荷连 `unsafe_load` 都解析不了，等于什么也没证明），再证明把加载器换成不安全的会变红；T025 跑 22 个变异（15 条规则静默 + 7 条设计明确不报的越界多报）全部被杀；**用 DSL 声明 invest v3 真实工作流（13 节点 / 13 连线），编译结果与 `build_v3.build()` 逐节点逐参数零差异**、校验零问题——比对中暴露并修了三处：条件 id 无法沿用（→ DSL 可写 `id`）、条件 `left_label` 与画布约定不一致（→ 按 `<节点显示名>/<变量>` 生成）、提示词里的字面量 `{{#` 被误报（→ 规则 #8 收窄）
- **Wave 3 G8 第一轮（2026-11-21，codex，41/50）**：F1 规则 #9 对编译产物永远不报——编译器把模板默认值（`model_id: ""`）全填上了，"键存在"恒真；实测 `BishengLLM` 对空 `model_id` 抛 `NoLlmModelConfigError` → 增 `param_empty`（只针对 llm/agent/rag 的 `model_id`，空提示词是合法的空提示词，不一刀切）；F2 `cases` 里写成平台字段名（`left_var` / `comparison_operation` / `operator`）被静默忽略 → 编译出 `null` → 引擎 pydantic 报错——编译器改为拒绝未知键、要求 `left`+`op`、`value`/`ref` 互斥、`value: null` → `""`，规则 #12 要求条件 `id` 与四字段为字符串；F3 平台列表畸形 / 非字符串类型抛原始异常 → 改报 `CliError` / 问题项；F4 覆盖层可追加与已声明节点同 id 的节点 → `node_duplicated`；F5 规则 #6 补查来源节点；F6 规则 #8 扫到了引擎从不替换的 `code` / `report` 参数（误报）→ 按参数类型限定范围；F8 `note` 节点豁免、顶层未知键报错。**不采纳 F7**（条件 case 无出边）：引擎把命中的 case 路由到空列表后静默结束、不报错，"命中即停"可能是作者本意——按 AC-18 不拦；第二轮 codex 用 langgraph 实测确认这条不采纳成立
- **Wave 3 G8 第二轮（2026-11-21，codex 深度，42/50）**：N1（High）**单键引用 `{{#histories#}}` / `{{#query#}}` / `{{#context#}}` 在工作流里必崩**——design §4.2(c) 把它们写成"特例键"是照搬 `prompt_template.py` 过时的文档字符串，我实现时没有自己验证；实测 `get_variable_by_str('histories')` → `IndexError`，已改为报 `reference_without_node`，原先把它们当合法输入的用例同步改掉；N2/N3 `output_msg`/`message` 与 `code_input`/`code_output` 的形状；N4 case `id` 非字符串；N5 `logic: AND` 会被引擎当成 OR → 只认小写 `and`/`or`；N6 模板内部畸形 → `CliError`；N7/N8/N10 规则 #8 漏扫 `output_result.value`（`type=input` 时被替换）、工具参数（引擎按名字全部替换，不看类型）、`image_prompt` 与 `code_input` 的 `ref` 变量——全部补上。**另做了随机模糊测试**（畸形结构 × 3000 次 × 20 个种子，覆盖编译 / 校验 / 覆盖层检查）：前后共找出并修掉 11 处"抛原始异常而不是报告"的位置，最终 0 次意外异常；变异探针 30+ 条全部被杀，其中一条变异因签名不匹配而"红得不对"，修正后确认红的正是那两条针对性用例
- **Wave 4 偏差（2026-11-22）**：① **模板缓存不带"平台版本指纹"**——design 决策 2 写了"带平台返回的版本指纹"，但 v2 模板端点只回列表，`/dev-toolkit/versions` 也没有平台版本号，平台根本不发这个指纹。改为：缓存条目按归一化 `base_url` 分条，记 CLI 版本、取回时间、内容 sha256；四项都成立且未超过 `TEMPLATE_CACHE_TTL`（1 小时）才命中，其余一律回平台重取，**重取失败即失败、旧条目不升格为兜底**（AC-05）；`refresh=True` 绕过缓存。模板是后端常量、只随平台升级变化，1 小时是"升级后最久多久才被察觉"的上界；② **读失败三态落在客户端层**：平台对不存在回 404（`NotFoundError`）/ 10500（版本），对无可见权限回 403（FGA）——三态可如实区分，按场景翻成不同退出码与下一步，`details` / `hints` 原样透传；105xx 的全局退出码映射仍按计划在 T040/T041（Wave 5）；③ **diff 比较表多纳入 `data.type`**（同 id 换了节点类型），按决策 5"新增即纳入"；参数值按规范化 JSON 比较（`7`≠`"7"`、`True`≠`1`，dict 键序不计）；连线按多重集合计数；④ **人读呈现**：长值从第一处不同的字符前 40 字起截取（从头截会把真正改动的那句截掉——用 invest v3 真实提示词复现过），完整值见 `--json`；⑤ `compiler.resolve_tool` 公开化，`FlowClient.tool_key` 复用同一解析器，不另写一份；⑥ **验证**：新增测试 82 个；变异 38 条全部被杀（中途两条存活：读失败翻译后丢 `details`、单边长参数不截断——均补测试后被杀）；diff 模糊测试 20 种子 × 3000 次（3.2 万次完成比较、2.8 万次按畸形拒绝、0 次意外异常）；真实数据：DSL 编译的 invest v3 与 `build_v3.build()` diff 为一致，叠加 4 处模拟网页改动（改提示词、改名、删连线、加节点）+ 拖画布 + 改文案，正好报出 4 处
- **Wave 4 G8 第一轮（2026-11-22，codex，40/50）**：采纳——批注节点（`data.type == "note"`，引擎跳过、DSL 无此类型）原先会被报成"线上独有节点"，改为单列提示、不计入差异；`data.id` 未比较（引擎按 `data.id` 建图，改了它 diff 却说一致）→ 纳入；`group_params: null`（`BaseNodeData` 允许）被当成平台缺陷 → 视为无参数；无信封的 404 被报成"工作流不存在" → 实为路由不存在（平台过老 / 地址错），改报 `EXIT_PLATFORM_TOO_OLD`；缓存临时文件用固定名，并发写可能装入半截文件 → `mkstemp` + 清理；10599 只在 v1 端点出现 → 从 v2 的拒绝码里删掉；工具列表的子项必须是对象。**不采纳**：未在 YAML 声明 `name`/`description` 时不比较它们——决策 5 明确纳入，push 确实会改掉线上显示名，diff 就该报（交用户复核）；10525/10529 的退出码按计划在 T040/T041 统一登记
- **Wave 4 G8 第二轮（2026-11-22，codex 深度，39/50）**：H1 信封里 `status_code` 不是整数时 `parse_envelope` 当成功 → 一次被拒的写会被报成成功（AC-27/29），改判契约缺陷，且写操作回包逐个查形状（新版本必须有整数 `id`）；H2 非流式运行 240 s 超时被报成"平台不可达" → 单独报"这一轮运行超时"（`EXIT_WAIT_TIMEOUT`），超时可由调用方指定；**根治（流式运行、先拿到会话标识）留给 T037**；M1 运行失败另立分类（无权时说"使用"权限，不说"编辑"）；M2 增 `diff.online_only_items`，给 push 的 AC-26 判定用，不能用 `count()`；M3 NaN/Infinity 会让 `--json` 输出非法 JSON → diff 拒绝；M4 缓存键改为 `base_url` 摘要（地址里的账号密码不落盘），缓存目录自带 `.gitignore`；M5 版本号只认 ASCII 数字（`"²".isdigit()` 为真但 `int` 崩）；L1 `edges: null` 视为无连线；L3 缓存文件超 16 MB 视为未命中；L5 补 FastAPI 风格 JSON 404 的用例。**不采纳**：L4 版本列表 `total`（Wave 5 `--list-versions` 需要时再加）、L6 强制 https（既有 login 的范围，不在本 feature）。**两轮修复后**：变异 59 条全部被杀（期间 3 次锚点因重构失效，按规矩搬锚点、未删变异）；diff 模糊测试 6 万次 0 次意外异常；invest v3 真实数据对照仍为一致；CLI 全量 786 通过，失败仍为既有 6 条
- **Wave 5 偏差（2026-11-22）**：① **T017 与 T031 同批落地**，`flow` 为两 / 三 token 命令（`flow tool register|list`），8 个叶子全部镜像全局 flag，T016 的参数化契约测试自动纳入；② **AC-32 需要后端配合（用户 2026-11-22 选定）**：v2 invoke 原先丢弃全部 `node_run`，失败节点只出现在被丢的那条事件里 → `PublishedWorkflowService.iter_events` 只放行"带 reason 的结束事件"，转换时只带节点标识与原因；③ **`flow run` 改走流式 invoke**（Wave 4 G8 H2 的根治）：会话标识随第一个事件到达；`--wait-timeout` 是等下一个事件的上限；非流式回包仍可解析（平台拒绝时的信封）；④ **续跑跨进程**：无人值守时停在等待输入即 exit 0，给出 `--resume r1.<base64url>` 凭据（工作流 / 会话 / 等待消息 / 节点 / 字段名，严格校验），并新增 `--input-json`（表单多字段一段文本填不了）——design §6.4 原面只有 `--input` / `--input-file`；⑤ **push 的"线上独有"以本检出上次推送的定义为共同祖先**（`.bisheng/cache/last_push.json`，design 决策 4 补充）：否则每次改参数再推都要确认，flag 会被无条件带上；⑥ **绑定文件位于声明文件旁**（不随 CWD），按平台 → 声明文件名索引；`diff` / `push` / `run` 在显式 `--flow-id` 且读取成功后写绑定；⑦ `push` 与线上全等时不建版本；`--create` 走 `POST /api/v2/flows`（平台同时建出当前版本并回 `version_id`）；⑧ `tool register` 按注册端点允许的键裁剪解析草稿并去掉空值（注册体 `extra="forbid"`，且显式 `null` 会盖掉平台默认值）；⑨ 10599 按 T041 注册为 `EXIT_FORBIDDEN`，客户端层不再加"无权修改"措辞（v1 专用码）；⑩ **验证**：新增 CLI 测试约 120 个 + 后端 6 个；CLI 全量 909 通过（失败仍为既有 6 条）；后端 `test/open_api` 改前改后失败集合逐条相同（44 条既有）；变异 51 条全部被杀（第二轮修复后一条存活：客户端"信封必须带事件列表"被命令层新加的"一轮必须以 close 或等待输入收尾"遮蔽，补了直指客户端那条规则的见证用例后被杀）
- **Wave 5 G8 第一轮（2026-11-22，codex，42/50）**：采纳——H1 资源归属人不可用（平台以 HTTP 404/500 + 固定文案拒绝）被翻成"工作流不存在，重新 push --create" → 按文案精确识别，报 `EXIT_FORBIDDEN` 与"没有可用的资源归属人"（AC-43）；H2 免确认时结果报 `online_only_dropped: 0` 而人读说有 N 处 → 照报条数并加 `online_only_confirmation`；M5 注册回包没有操作仍报成功 → `EXIT_DEFECT`；M6 `flow.json` 改为原子替换；L7 `run` 不检查声明文件存在 → 拒绝；L8 续跑与绑定冲突的提示不可执行 → 给出 `--flow-id <凭据所属>`；L9 默认版本名秒级会撞 10502 → 加随机后缀。**部分采纳** M3（last_push 无完整性保护）：只在与线上语义全等时才被采信，损坏只会多问一次，不加校验和；文档里"machine-local"改为"本检出、不进 git"。**不采纳** M4（失败原因可能含输入）：同一段 `str(e)` 早已随收尾错误事件 `data.exception` 下发给 v2 调用方，不新增暴露面，改为在 design §6.1 登记事件增量；L10（last_push 存整份定义）：每个工作流一条，内容即仓库里 YAML 的编译结果
- **Wave 5 G8 第二轮（2026-11-22，codex 深度，40/50）**：采纳——H1 一轮既无 `close` 也未停住却报"运行完成"（含信封缺 `events`） → `EXIT_DEFECT`；H2 `push --create` 平台已建工作流、本地绑定写失败却崩成 exit 1 且丢掉 id → 绑定写入降为告警并给出 `--flow-id`，结果带 `binding_saved`；M3 `!include` 可读仓库外文件并随 push 上传 → **推翻 design 决策 7 的"不禁止"**，限定在声明目录树内；M4 线上取值回显时第三方模型密钥（`sk-…`）不被遮蔽 → 全局掩码新增 `sk-` 规则（≥20 字符，不误伤 `sk-learn`）；M5 平台帧缺会话标识时发出自己读不回的续跑凭据 → `EXIT_DEFECT`；L6 `--create` 回包缺 `version_id` 仍报成功 → 纳入必填；L7 用户停止被报成节点失败 → `EXIT_CANCELLED`。**未纳入本次提交**：L8 工作树里的 `probe_*.py` 是 11-21 codex 审查遗留，删除待用户确认。第一轮修复全部经第二轮复验通过
- **Wave 6 偏差（2026-11-22）**：① **T044 已在 Wave 2（`ad1aef4845`）随 scope 注册表一起交付**，三语言文案齐全，本 Wave 只补勾；② T001 的 live 对照原设计为"前端文件不在就退场"，但 T042 只删常量、文件仍在 → 改为**有正面证据才退场**（常量不在 **且** `getWorkflowNodeTemplate` 本体请求后端接口），并新增常驻断言 `test_frontend_keeps_no_template_copy`；③ `getWorkflowNodeTemplate` 除"非空列表"外**逐项校验** `type` 字符串 + `group_params` 列表（G8 第二轮：形状不对的模板会在画布拖拽回调里才炸）；失败文案用共享的 `api_errors:fallback`（带默认值，i18n 未就绪时不露键名）；④ 节点面板加载失败显示 `role=alert` 原因 + 重试按钮；`getToolTree` 缺 `node_template` 的工具不再静默消失，工具页签点名（新键 `nodeTemplatesLoadFailed` / `toolTemplatesUnavailable` / `retry`，三语言同批）；⑤ 按 `src/frontend/AGENTS.md`"谁改谁还"：触碰的 `workflow.ts` / `flowUtils.ts` 里残留的中文字面量（开发者日志与只进 console 的拒绝原因）改英文，`eslint-suppressions.json` 只减（Chinese-literal 24+4 条清零，Sidebar no-unused-vars 7→6）；Sidebar 里 `['时间','计算器','代码执行器']` 是按数据里的预置工具名过滤、不是界面文案，未动；⑥ 工具节点输出参数 `label` 由前端的 `输出变量` 变为后端的 `output`：画布按 i18n 键 `node.tool.output.label` 渲染标题（三语言都有），用户看不到 `output`（G8 两轮均复核）；⑦ **验证**：platform vitest 改前 41 条失败 / 改后 40 条（一条无关用例转绿，疑似不稳定），**无新增失败**；新增前端测试 19 个 + 后端守卫 9 个；变异 14 条全部被杀；后端模板与被删字面量逐项深相等、顺序与 `v` 一致（codex 复核）
- **Wave 6 G8 第一轮（2026-11-22，codex，43/50）**：采纳——守卫只按整串匹配（换引号即误红、留在注释里即误绿）→ 限定在函数体内、容忍引号；缺模板工具只进 console → 工具页签点名；开发者文本直达用户 → 共享兜底文案；加重试按钮；文案改写（不用"节点库"这一新词）。**不采纳**：知识库快捷建流复用查询缓存（一次性动作，取新的更对）；返回值类型化（消费方都是 `@ts-strict-ignore` 遗留文件）；翻译遗留中文注释（超出本 Wave）
- **Wave 6 G8 第二轮（2026-11-22，codex 深度，41/50）**：采纳——M1 单引号 / 无空格写的副本能骗过"不留副本"守卫（codex 用真实辅助函数实测）→ 前后端检测都改为任意引号、不依赖空格，并各自补检测器自身的正反用例；L3 守卫对 `export async function` 与先出现 `"https://…"` 字符串的写法误红 → 接受两种声明、去掉不识字符串的注释剥离；L4 形状不对的模板在拖拽回调里才炸 → 逐项校验；L5 i18n 未就绪时露出键名 → 默认值 + 显式兜底（实测未初始化的 i18next 连 `defaultValue` 都不返回）。**未处理并登记为已知短板**：M2 守卫与 platform vitest 不在任何 CI（需用户决定是否接入）；L6 / L7 Sidebar 两处既有问题。复核确认：无回归（模板与旧字面量深相等）、拉取不会改到编辑中的节点（三条建节点路径都深拷贝）、无 XSS（插值落在 React 文本节点）
