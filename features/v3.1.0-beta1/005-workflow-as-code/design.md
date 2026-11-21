# Design: 工作流即代码（YAML 声明 + CLI 命令族 + 节点模板后端化）

> **本文档定位 — 现状快照（Why this How）**
>
> - `spec.md` 回答 **做什么**（目标、AC、边界）
> - `design.md`（本文）回答 **为什么这么实现**：关键决策、运行时不直观的事实、对外契约
> - `tasks.md` 是 **流水账**：拆了哪些任务、做了什么改动
>
> 调整原则（详见 `docs/SDD-Guide.md` §3-§4）：
> - 实现变化 → **覆盖更新本文档**，只留"今天的状态"、不留旧快照；但每个决策保留"为什么 + 被否方案"和坑
> - **偏差分级**：推翻已 ★ 确认的决策 → 停下与用户重新确认；纯实现细节 → 直接改 design，不必停

**关联**: [spec.md](./spec.md) · [tasks.md](./tasks.md)
**版本**: v3.1.0-beta1
**最后更新**: 2026-11-20

---

## 1. 目标与非目标

### 目标

把"工作流定义"从**平台网页画布里的一条记录**变成**仓库里一份可评审、可 diff、可回滚的文本**，并让毕昇自己提供这件事所需的底座：节点模板由后端持有并发布、工作流写操作有一条专用的开放接口面、命令行工具有一条 `flow` 命令族。

本 feature 在系统里扮演的角色是**平台能力**，不是某个使用方的脚本：CLI 侧的 YAML DSL 与 diff 算法是随 CLI 包发布的通用件，后端侧只提供"节点模板的权威定义"与"工作流写入的受控通道"两件事。

### 非目标

- **不提供工作流的可视化编辑**。画布交互一行不改，本 feature 只增加一条平行的声明式入口。
- **不改工作流的运行时语义**。节点行为、图编译规则、迭代上限、变量替换规则全部原样。声明式只是**又一种产生同一份 `data` 的方式**。
- **不做自动上线**。版本建好为止，上线仍是人工动作。
- **不做"指定他人身份"**。写操作以凭据自身的资源归属人身份进行。
- **不让 CLI 读任意文件 / 执行任意命令**。CLI 的文件面只到"读声明文件与其引用的提示词文件"。
- **不替代网页画布**。两者长期并存：画布改的会以 diff 形式被看见，声明改的会在画布里被看见。

---

## 2. 关键约束

全局架构铁律（双 DB / 多租户 / 权限 / 分层 / 错误码）遵循 `docs/constitution.md` C1–C8，此处不重抄。本功能**特有**的硬约束有五条：

### 约束 1：CLI 与后端包零耦合，且依赖预算只有两个

`src/bisheng-cli/pyproject.toml` 的 description 明写 `zero coupling to the backend package`，依赖只有 `httpx` 与 `PyYAML`（`requires-python >= 3.11`）。因此：

- CLI **不得 import 后端任何模块**，包括后端新增的节点模板模块；
- 本 feature **不新增任何第三方依赖**（`PyYAML` 已在，编译与 YAML 解析零成本）；
- 因此凡是"后端算好了给 CLI"的东西，CLI 都只能**经 HTTP 取**，不能 import。

这条约束直接决定了编译器必须住在 CLI 侧（决策 1）。

### 约束 2：CLI 的凭据模型只有"服务账号密钥"一种

`src/bisheng-cli/bisheng_cli/credentials.py` 只存 `bs-sak-*` / `bs-pat-*`，落在 `~/.bisheng/credentials.json`（`os.open(..., 0o600)` 单步创建，明文 key，**文件模式就是保护**）。后端 v1 接口要的是 JWT（cookie `access_token_cookie` 或 `Authorization: Bearer <JWT>`），`bs-sak-*` 不是 JWT，`decode_jwt_token` 解不了。

毕昇自己的登录是自研的（`GET /user/get_captcha` 给 captcha_key、验证码答案只存在于后端容器日志、密码要 RSA 加密）——那是**本机自有实例**的路径，不是通用路径。

→ **凡 CLI 要用的后端接口，必须在 `/api/v2` 面上。** 这不是风格偏好，是可达性的硬约束。

### 约束 3：上游三条"CLI 不做"的原则

`src/bisheng-cli/bisheng_cli/project.py` 文件头、`commands/logs.py` 文件头、`output.py` 文件头各写了一条，本 feature 逐条对应：

| 上游原则 | 本 feature 的落实 |
|---|---|
| 清单 schema 归服务端；CLI 只做"存在 / 可解析 / 必填字段"的本地快检，**不填默认值** | 节点模板的**结构**归后端所有（后端发布，CLI 不造）；CLI 的 YAML 快检只查顶层字段与可解析性，不替后端裁定节点类型合法性 |
| CLI **不做权限判定** | diff / push / run / tool 的权限判断全在服务端；CLI 不预判所有权、不缓存权限结论，只把服务端错误码翻成人话 |
| 掩码挂在**进程出口** | 差异报告若含源码或凭据，走 `output.mask()`（已覆盖 `bs-sak-*` / `bs-pat-*` / `Authorization: Bearer`），不是每个调用点各自脱敏 |

### 约束 4：`/api/v2` 面的端点不得自行解析身份

`src/backend/bisheng/api/router.py:158-171` 的注释写死了：`router_rpc` 只有一个 router 级依赖 `verify_open_api_access`，它认证 bearer 后从端点上的 `@open_api_scope` 标记读 scope 与身份模式；**没有标记的端点 fail closed**（`OpenApiEndpointUnregisteredError`）。因此每个 v2 子路由的每个端点都必须：

1. 挂 `@open_api_scope("...")`（scope 未在 `open_api/domain/scopes.py` 注册时，装饰器在 **import 期**抛 `ValueError` —— `scopes.py:280`）；
2. 身份从 ContextVar 读 `get_current_open_api_principal()`，**不得**自己解析 header / token。

### 约束 5：上游 CLI 的三条铁律

`src/bisheng-cli/tests/conftest.py` 文件头：

1. **任何测试都不许碰网络** —— `no_network` fixture 把 `httpx.HTTPTransport.handle_request` 换成抛 `AssertionError(f"unmocked network call: {method} {url}")` 的哨兵；
2. **任何测试都不许碰真实 home 目录** —— `home_dir` fixture 把 `Path.home()` 指向 `tmp_path`；
3. **任何长 key 字面量都不许写进 `.py`** —— `FAKE_KEY` 用拼接构造。

外加两条 CLI 结构铁律（`cli.py` 文件头 + `main.py` 文件头）：

- **命令要么注册要么不存在，绝不注册桩** —— 注册了却回答"本版本没有"会误导 agent；`DEFERRED_COMMANDS` 是"公告了但没注册"的名单，由 `announced ≠ registered` 的测试守住形状。
- **每条退出路径恰好发出一个 `result` 事件**（成功、已处理的失败、崩溃 alike）。

### Constitution Check（逐条核对 `docs/constitution.md` C1–C8）

| 条款 | 本 feature 是否触碰 | 结论 |
|---|---|---|
| **C1** DDD 分层 `Router → Endpoint → Service → Repository → DB` | 触碰：新增 v2 端点 | **合规**。端点只做"取身份 → 调 service → 包装响应"，业务判定全部在既有 `WorkFlowService`（`api/services/workflow.py`）里，端点**不 import `database.models`**（RULE-3）、**不新加 DAO 入口**（C1 明文）、**不写 ORM 查询**。RULE-5 检查的是 `bisheng/<模块>/api/...` 之间的互导；新端点所在的 `bisheng/open_endpoints/api/...` 只 import `bisheng.api.services.*`（被 RULE-5 的排除项 `bisheng\.api\.` 命中）。**注意**：新端点**不得** import `bisheng.tool.api.tool`（那会命中 RULE-5 的 `bisheng.<模块>.api.`）；工具侧只 import `bisheng.tool.domain.services.tool` |
| **C2** 双库（MySQL + DM8） | 不触碰 | **合规**。本 feature **不新增任何 ORM 查询、不新增任何列**——写路径全部走既有 DAO（`FlowVersionDao.create_version` / `change_current_version`），读的表结构一字未动 |
| **C3** 多租户自动注入 | 触碰：写操作 | **合规**。端点不手写 `WHERE tenant_id = X`；归属人身份经 `get_open_api_operator_async()` 解析，该函数内部用 `UserTenant` 校验"归属人的活跃租户 == 当前租户"，不等则 404（`open_endpoints/domain/utils.py:57`、`:79`） |
| **C4** 权限：已验证业务目标 + 统一动作运行时 | 触碰：写路径的核心 | **合规**。判定一律走 `require_business_action(login_user, resource_type="workflow", resource_id=..., action="edit")`（C4 给出的标准形状，与 `WorkFlowService.judge_flow_write_permission` 内部用的完全一致）。**不查 `role_access` / OpenFGA / `permission_id` 模板**（RULE-8 / INV-T19），**不 import OpenFGA 基础设施**（RULE-9） |
| **C5** 错误码 `MMMEE` | 触碰 | **合规**。**本 feature 不新增错误码**——既有 105 段（`common/errcode/flow.py`）已覆盖全部要报的场景（§6.3）。若最终需要新增 105xx 码：文案必须同时落到 `src/frontend/packages/locales/src/api_errors/` 的**三种语言**（`check-i18n` 的 `Code:\s*int\s*=\s*(\d+)` 正则会扫到 105xx，缺文案即 CI 红） |
| **C6** 无硬编码密钥 | 触碰：CLI 凭据 | **合规**。CLI 不新增任何凭据文件——复用 `credentials.py` 的 `~/.bisheng/credentials.json`（0600）。本 feature 不新增 `password` / `secret_key` / `api_key` / `access_token` 字面量（RULE-7） |
| **C7** 前端 store 不直接调 HTTP | 触碰：前端改法 | **合规**。前端改动只在 `controllers/API/workflow.ts`（**API 封装层**）与 `util/flowUtils.ts`（纯函数）。**不动任何 store**，两个消费点（`pages/BuildPage/flow/Sidebar.tsx`、`pages/KnowledgePage/components/FileUploadStep4.tsx`）已经是走 `getWorkflowNodeTemplate()` 的 |
| **C8** 本地文件系统无共享状态 | 触碰：CLI 的本地文件 | **合规**。C8 约束的是**后端多进程**（API 副本 / Celery / Beat / Linsight worker 之间不靠本地磁盘共享状态）。本 feature 的本地文件全在 **CLI 侧**（单一进程、单一开发者）：`.bisheng/flow.json`（照 `.bisheng/app.json` 的既有形状）与 `.bisheng/cache/node_templates.json`（**纯缓存，可随时删掉重建**，不是真相源）。后端侧**不新增任何本地文件状态** |

---

## 3. 方案对比与选定

### 决策 1：编译器住在 CLI 侧，不住后端侧

- **备选**：
  - **A. 编译器在 CLI**（YAML → 工作流 `data` 全部在本地完成，编译结果作为普通 JSON 提交）
    - 优点：离线可编译；`bisheng flow compile` 不发请求（除了取节点模板）；后端不因新增 DSL 而膨胀；与"零耦合"一致。
    - 缺点：编译规则（哪些参数必填、哪种节点类型存在）若在 CLI 里写，就是**第二份**规则。
  - **B. 编译器在后端**（新增 `POST /api/v2/workflows/compile`，CLI 把 YAML 原文传上去，后端返回展开后的 `data`）
    - 优点：规则只有一份；DSL 改动不需要发 CLI 版本。
    - 缺点：**违反约束 1**（CLI 要么 import 后端，要么 HTTP 往返——后者等于"给 DSL 加 HTTP 端点"，与本次形态决策冲突）；离线编译消失；`flow diff` 要多一次往返才能拿到可比的本地形态。
- **选定**：**A**。
- **原因**：
  1. 约束 1 排除了 B 的 import 变体；HTTP 变体与"CLI 侧持有声明格式"这个已定形态直接冲突。
  2. B 把"编译"变成一次服务端调用，**编译期结构校验（AC-12~AC-18）也就跟着变成服务端行为**——但 spec 明确要求这些校验发生在编译期，且失败时要"在本地即拒绝"（AC-11 同源精神）。放在 CLI 才能在无平台连接时也给出确定结论。
  3. **后端目前没有节点级校验逻辑可复制**：`Flow.data` 的校验只到"是个 dict 且含 `nodes`/`edges` 两个 key"（`database/models/flow.py` 的 `validate_json`）。编译器不是在复制服务端的判断，而是在补上**服务端只在运行时才暴露**的那一层（见 §5 坑 2、坑 3）。复制不存在。
  4. 约束 1 的第二条（依赖预算）也支持 A：编译器作为 CLI 内部模块不引入新依赖。
- **规则的存在被 B 的优点指出来了，怎么化解**：节点**类型清单与参数骨架**（易变、量大）归后端发布，CLI **不硬编码**（决策 2）；节点**必填参数与结构规则**（稳定、少）由 CLI 持有，理由见上第 3 点——它们描述的是"这份 DSL 怎么读"，服务端目前不读 DSL，因此不存在"两份同义规则"。若将来后端开始**也读 YAML**（例如提供 `POST /api/v2/workflows/import-yaml`），那时才出现真正的双份，届时必须把规则上收到后端并由 CLI 持副本（照 `app_manifest.py` 的先例）。**触发条件：后端新增任何消费 YAML 的入口。**
- **何时该重新考虑**：见上面"触发条件"；或 CLI 侧规则与平台实际行为出现无法解释的偏差时。

### 决策 2：节点模板归后端所有，CLI 与前端都不留副本

- **备选**：
  - **A. 后端持有并发布**（新增 `GET /api/v1/workflow/node_templates` 给前端；新增 `GET /api/v2/flow/node_templates` 给 CLI；两者读同一个后端模块）
    - 优点：真相只有一份；前端那份约 890 行的 TS 字面量消失；invest 侧 `extract_templates.js`（用 `new Function` 求值前端源码）与 `bsflow.py` 的 `tool_tpl`（前端 `getToolTree` 的逐行 Python 复刻）都能删掉。
    - 缺点：前端首屏多一次请求；CLI 离线编译需要先取模板（见决策 2 的"缓存"小节）。
  - **B. CLI 内嵌一份模板常量**（照抄后端模块）
    - 优点：离线编译。
    - 缺点：**与 spec AC-05 直接冲突**（"模板接口不可用时不得回落到任何硬编码的模板副本"）；且这是字面意义上的第二份真相，正是本 feature 要消灭的东西。
  - **C. 维持现状**（前端留着 TS 字面量，CLI 自己想办法）
    - 优点：零改动。
    - 缺点：漂移的根源原封不动。
- **选定**：**A**。
- **原因**：
  1. spec AC-01 / AC-05 已经把"只有一份、且不回落到硬编码副本"定死。
  2. 后端已有放纯数据常量的先例（`workflow/common/const.py`、`workflow/common/condition.py`、`workflow/common/node.py`），新模块 `workflow/common/node_templates.py` 与它们同层。
  3. 选 Python 模块而不是 JSON 文件：能被 ruff / 类型检查管住，且能被后端自己的测试直接引用；JSON 文件会让"生成工具节点模板"的逻辑无处安放。
- **两个端点而不是一个**（这是约束 2 的直接后果）：
  - `GET /api/v1/workflow/node_templates` —— 给**前端**（浏览器用 JWT cookie；v2 面的 `verify_open_api_access` 不认 cookie，浏览器也没有 `bs-sak-*`）。
  - `GET /api/v2/flow/node_templates` —— 给 **CLI / 服务账号**（约束 2：CLI 只有服务账号密钥）。
  - 两者读**同一个** `workflow/common/node_templates.py`，不存在两份模板。
  - **⚠ 与 spec AC-01 的一处读法需要点明**：AC-01 的原话是"由后端持有并经**一个**只读接口发布……**不得在前端再保留第二份节点模板常量**"。本设计取它的**后半句（禁止第二份常量）**为硬约束——那是本 feature 存在的理由；前半句的"一个"若被读成"端点数量只能是 1"，则本设计需要让 CLI 改走 v1 JWT，而那**会推翻约束 2**（CLI 只有 `bs-sak-*`，v1 面它够不着），是明显更差的方案。**两个端点返回的是同一份数据、同一份权威，不构成"第二份"**。此处点明，供 design ★ 确认时一并裁决。
- **CLI 的模板缓存**：CLI 把取到的模板按平台 `base_url` 缓存到项目内 `.bisheng/cache/node_templates.json`，带平台返回的版本指纹。缓存命中省一次往返；缓存缺失或过期则重新取；**取不到就失败并给出可读原因，绝不回落**（AC-05）。缓存是**性能优化，不是副本**——它存的是平台刚发下来的原文。
- **AC-44（平台侧为声明格式的权威定义方）在本决策下的具体兑现**：
  - 节点模板（声明格式的主体）：后端是唯一权威，前端与 CLI 都从它取，**没有第二份需要同步的东西**。
  - YAML DSL 自身的键名（`nodes` / `type` / `params` / `edges` / `from` / `to` / `handle`）：本版**只有 CLI 一处实现**（§4.3 的 `flow/spec.py`），后端不读 YAML，因而没有第二份。CLI 对 YAML 的检查只到"顶层必填字段 + 可 `safe_load` + 顶层是映射"（照 `project.py` 的形状，不填默认值、不裁定节点类型合法性）。
  - 因此"平台侧与命令行工具在同一次变更中一起更新"在本版**没有需要同步的两份**。若将来后端新增消费 YAML 的入口，则该入口成为权威，CLI 改为持同源副本，且**必须与后端在同一次变更中一起更新**（照 `app_manifest.py` 里 `SUPPORTED_RUNTIMES` 那条 "F054 must change this constant in the same commit" 的写法，在两处各留一条互相指向的注释）。
- **何时该重新考虑**：节点模板的体积或生成成本上升到"每次编译取一次不可接受"时（例如模板需要按租户/权限差异化生成），那时才考虑客户端缓存副本 + 契约测试。

### 决策 3：写路径 = 新增 `workflow:write` scope + v2 端点，业务逻辑复用既有 v1 service

- **备选**：
  - **A. 新增 `workflow:write` scope + 一组 `/api/v2/flows/**` 端点，端点内部调用既有的 `WorkFlowService`**
    - 优点：与上游自己的先例一致（`/api/v2/apps/**`，F055 给 `bisheng deploy` / `bisheng logs` 建的通道）；凭据模型不分裂；权限语义与 v1 完全一致（因为调用的是同一个 service）。
    - 缺点：要动 `open_api/domain/scopes.py`、加端点、可能要同步开放 API 契约文件。
  - **B. CLI 走 v1 用户态 JWT 登录**（复刻 `tools/bisheng/bs.py` 那套 captcha + RSA 流程）
    - 优点：后端一行不改。
    - 缺点：**只适用于本机自有实例**（验证码答案在后端容器日志里）；给 CLI 引入第二种凭据类型，与 `credentials.py` 的单模型冲突；上游明确反对。
  - **C. 复用 `app:manage`**
    - 缺点：该 scope 的 group 是 `local_dev_toolkit`（`scopes.py`），语义是"本地开发工具"；工作流写是平台级能力。而且 `app:manage` 是 `requires_open_platform=True`（开放平台开关打开才可签发），会额外增加部署前置条件。
- **选定**：**A**。
- **原因**：
  1. 约束 2 排除了 B（CLI 只有服务账号密钥，v1 面它够不着）。
  2. C 的语义与部署前置都不对。
  3. **A 有一个原先没被发现的捷径**：后端已经存在"principal → `UserPayload`"的桥 —— `open_endpoints/domain/utils.py` 的 `get_open_api_operator()` / `get_open_api_operator_async()`。它做的正是"服务账号以其资源归属人身份行事"这件事，而且**已经把该拒绝的情况拒掉了**：
     - `_principal_user_id()` 取 `principal.effective_user_id or principal.resource_owner_user_id`；两者都 `None` 时抛错（→ AC-43）；
     - 服务账号（`actor_kind == "service_account"` 且 `mode == "S"`）时 `force_non_admin=True`，返回的 `UserPayload` **角色列表为空、`is_global_super=False`**（→ 服务账号拿不到管理员的隐式放行）；
     - `hosted_app` 一律拒（`OpenApiHostedAppEndpointRefusedError`）；
     - 归属人必须存在、已启用、且其活跃租户必须等于当前租户，否则 404。
     
     于是 v2 写端点可以**直接把 `get_open_api_operator_async()` 的结果喂给既有 v1 service**——`WorkFlowService.judge_flow_write_permission(user, flow_id)` 内部就是 `require_business_action(user, resource_type="workflow", resource_id=..., action="edit")`（`api/services/flow.py:125-136`）。**权限语义与网页端逐字一致，不需要新写一份授权逻辑，也不需要新发明"服务账号能不能改这个工作流"这套关系。**
  4. 这条捷径解掉了本 feature 原先的最大风险项（见 §8 的"已解除的风险"）。
- **scope 的划分**：

  | scope | 覆盖 | 为什么这么划 |
  |---|---|---|
  | `workflow:read`（**已存在，扩展**） | `GET /api/v2/flows/{flow_id}`（已有）、`GET /api/v2/flows/{flow_id}/versions`、`GET /api/v2/flows/{flow_id}/versions/{version_id}`、`GET /api/v2/flow/node_templates`、`GET /api/v2/tools` | 都是读。版本读取与节点模板读取是 diff / compile 的前提，与"读工作流"同一族 |
  | `workflow:write`（**新增**） | `POST /api/v2/flows`、`POST /api/v2/flows/{flow_id}/versions`、`PUT /api/v2/flows/{flow_id}/versions/{version_id}`、`POST /api/v2/flows/{flow_id}/versions/{version_id}/activate`、`POST /api/v2/tools`、`POST /api/v2/tools/schema` | 这就是 spec AC-38 要求的"专用于写工作流的权限位" |

  - **工具注册为什么也归 `workflow:write`**：工具注册是"让工作流能引用某个操作"的前置步骤，与工作流写入同属一次开发动作；拆成第二个 scope 会让一把 CLI 密钥必须同时勾两个位才能跑完 `compile → push` 流程。而 spec AC-38 只要求"承载工作流写操作的凭据须持有一个专用于写工作流的权限位，且不得以其他权限位替代它"——`workflow:write` 正是那个专用位，没有任何其他权限位能替代它。
  - **什么时候该拆**：当"只读工作流但允许注册工具"成为一类真实需求（今天不是），或者工具注册需要与工作流写入不同的签发策略时，拆成 `tool:write`。
  - **`scope` 注册表里的 `endpoints` 元组是声明性的**，实际强制在端点上的 `@open_api_scope` 装饰器（约束 4）。两处都要同步改：注册表新增/扩展 `OpenApiScope` 条目，端点挂装饰器。装饰器在 scope 未注册时 **import 期**抛 `ValueError`（`scopes.py:280`），所以顺序不能反。
  - **模式**：写端点用 `modes=("S",)`（仅服务账号模式），与 `app:manage` 同（`open_endpoints/api/endpoints/deploy.py` 的 `_MODES`）。委派模式（D）意味着"代别人写"，本 feature 明确排除（AC-42）。
- **何时该重新考虑**：若上游最终把工作流写并入既有的 `app:manage` 或另立一个 `flow:*` 族，则 scope 划分需随之调整；端点路径与业务复用方式不变。

### 决策 4：`flow push` = 新建版本 **+** 切当前版本

- **备选**：
  - **A. 建新版本并切为当前版本**（默认；`--no-activate` 可只建不切）
  - **B. 只建新版本**，切换当前版本交给人去网页点
- **选定**：**A**。
- **原因**：
  1. **不切就没有效果**。`POST /api/v2/workflow/invoke` 跑的是 `Flow.data`（`open_endpoints/api/endpoints/workflow.py` → `PublishedWorkflowService.prepare_invocation` → `callback.set_workflow_data(workflow_info.data, ...)`），而 `Flow.data` 是**当前版本 data 的镜像**——`FlowVersionDao.change_current_version` 里那句 `parent.data = target.data`（`database/models/flow_version.py:274`）就是这条镜像的写入点。只建不切，`flow run` 跑的还是旧定义，"改 YAML → 看差异 → 推送 → 跑一次"这个闭环会**跑到旧版本上去**，而且 diff 会永远显示"本地与线上不同"。
  2. **切当前版本不违反 AC-25**（"不覆盖或改写任何既有版本"）：`change_current_version` 只改 `is_current` 标志位，并把 `Flow.data` 镜像成目标版本的 data；**任何版本的 `data` 都不被改写**。
  3. **AC-27 在这里落地**：平台的 `WorkFlowService.change_current_version` 在工作流处于上线状态时返回 `WorkFlowOnlineEditError`（10525，"Workflow is live and not editable"，`api/services/flow.py:150-151`）。因此 push 在上线工作流上的表现是：**版本已建好，但切换被平台拒绝 → push 必须报出这个状态与下一步（先下线），且不得报"推送成功"**（AC-27 的原文正是这个意思）。
  4. `--no-activate` 保留，是因为"先把版本建好、评审完再切"是真实的工作流节奏。
- **线上独有内容的处置（AC-26）**：push 前必跑 diff。若线上有本地没有的节点 / 连线 / 参数取值：
  - 交互式：列出这些内容并要求确认；
  - **无 TTY：拒绝**，要求显式带上 `--allow-drop-online-only`（沿用 `cli.py` 的 `confirm()` 形状：`assume_yes` 为假且非 TTY 时抛 `CliError(EXIT_USAGE)`，而不是默认同意）。
  - 这些内容**保留在旧版本里**（旧版本的 `data` 一字不改），只是不再出现在新版本里、也不再被当前版本引用。
- **何时该重新考虑**：若平台将来允许"上线状态下切当前版本"，则 AC-27 的拒绝语义需要重写；本 feature 的 push 逻辑（先建后切）不变。

### 决策 5：diff 排除画布信息，但纳入节点语义字段

- **备选**：
  - **A. 只比"执行语义"** —— 比节点集合、参数取值、连线；排除 `position` / `viewport` / `v`
  - **B. 比整份 JSON** —— 任何字节不同都算差异
- **选定**：**A**，但**边界比"只比执行语义"更宽**（见下）。
- **原因**：
  1. `position` 与 `viewport` 是画布坐标与缩放，不进图编译逻辑（`graph_engine.py` 只读 `nodes` / `edges`），改它们不影响运行。spec AC-21 明确排除。
  2. 但**不能只比参数值**。节点 `data` 里还有几个字段是**有语义的**：`tab.value`（`input` 节点的 `dialog_input` 与表单输入是两种不同节点）、`tool_key`（决定工具节点调哪个工具）、`name`（画布上的显示名）、`description`。这些变了都应报出来，否则"本地与线上看起来一样、跑起来不一样"。spec §3 边界情况里明确写了这条："当差异中出现仅参数取值不同、而该参数在平台上不参与执行时，仍按差异报出——平台是否使用某参数是平台的事，差异工具不替它判断。"同样的道理反过来也成立。
  3. `v`（节点版本号，`BaseNodeData.v`）排除：它是前端 `flowCompatible.ts` 的迁移版本号，与工作流行为无关。
- **比较规则（design 层面的契约）**：

  | 比较对象 | 键 | 忽略 |
  |---|---|---|
  | 节点集合 | `node.id` | — |
  | 节点参数 | `data.group_params[].params[].key` → `value` | `label` / `desc` / `type` / `required` / `placeholder` / `options` / `help`（这些是模板元数据，不是开发者填的值；模板整体变了由节点模板版本体现，不进 diff） |
  | 节点其他字段 | `data.name` / `data.description` / `data.tab` / `data.tool_key` | `data.v` |
  | 连线 | `(source, sourceHandle, target)` 三元组 | `id` / `animated` / `type`（`id` 是前端生成的 `xy-edge__...`，随 `sourceHandle` 变，不是独立事实） |
  | 顶层 | — | `viewport`、节点的 `position` |

  **参数比较必须按 key 而不是按位置**：`group_params` 是分组数组，开发者加一个参数会让分组位置整体挪动；按位置比会把"没改"报成"全改了"。
- **何时该重新考虑**：平台若给 `data` 引入了更多与执行相关的字段（例如某个节点的"重试次数"），需要同步扩这张表——扩的方向是"新增即纳入"，因为漏纳入的代价（看不出真实差异）远大于误纳入（多报一条）。

### 决策 6：Python 覆盖层收窄到"只覆盖已声明参数 + 只追加未声明节点/边"

- **备选**：
  - **A. 收窄**（YAML 是唯一真相源）
  - **B. 允许 Python 完整改写**（YAML 只是初始值）
- **选定**：**A**。
- **原因**：
  1. **diff 的可信度依赖"YAML 是真相源"**。若 Python 能删改 YAML 已声明的结构，两份描述会互相矛盾，diff 的语义立刻塌掉——而 diff 正是本 feature 存在的理由。
  2. spec AC-08 已经把这条写死：覆盖层试图删除或改变 YAML 已声明的节点与连线结构时，**拒绝并说明被拒的具体项**。
  3. 覆盖层仍然有用：把"这个环境的模型 id 是 7"这类**环境差异**从真相源里摘出去，同时不引入第二份结构描述（spec AC-09 允许"开发者显式声明的环境差异"）。
- **具体语义**：

  | 覆盖层能做的 | 覆盖层不能做的 |
  |---|---|
  | 覆盖 YAML 已声明节点的**已声明参数**的取值 | 新增 YAML 未声明的参数键 |
  | 追加 YAML 未声明的**节点** | 删除节点 |
  | 追加 YAML 未声明的**连线** | 删除连线 |
  | — | 改一条已声明连线的 `sourceHandle` / `target` |
  | — | 改节点的 `id` / `type` |

  被拒时**逐项列出**（哪条声明的哪个参数、哪条已声明的节点/连线），不只报一句"不允许"。
- **覆盖层的形态**：一个 Python 文件，导出 `apply(spec) -> spec`（或直接给出要改的字典）。CLI **import** 它执行——**这是执行用户代码**，与 AC-10 的精神有张力（"不以任何方式执行声明文件中的内容"）。
  - **区分**：`flow.yaml` 是**声明**，只准 `safe_load`（AC-10）；覆盖层是**代码**，用户显式指定 `--overlay overlay.py`，执行它是用户自己的意图。
  - 但这仍是一条需要写进 design 的边界：**覆盖层文件是唯一会被执行的代码**，它不在 "YAML 不执行" 的保护范围内。覆盖层能做什么由上面的表限制住——**它在数据层面受限，不在代码层面受限**（它能 `import os`，我们管不住；所以文档必须说清"覆盖层是可信代码"）。
  - **为什么保留**：用户已明确决定保留并做窄。收窄的是**它对工作流结构的影响力**，不是它的能力边界。
- **何时该重新考虑**：若 YAML 表达力足够（把所有需求都能写进 YAML），覆盖层可以整体删掉——这是最干净的终局。保留它是因为本次范围内有"同一份声明在不同环境用不同模型 id"这类需求。

### 决策 7：YAML 解析用 `SafeLoader` 子类，`!include` 是唯一新增的构造器

- **备选**：
  - **A. `yaml.safe_load` + 自己处理 `!include`** —— 但 `safe_load` 遇到未知标签会抛 `ConstructorError`（"could not determine a constructor for the tag '!include'"），所以"自己处理"必须换一种加载器
  - **B. `yaml.load(..., Loader=yaml.SafeLoader)` 同义；或用 `yaml.FullLoader` / `unsafe_load`（**拒绝**）
  - **C. 不用自定义标签，改用普通字符串约定**（如 `system_prompt: "@prompts/system_code.md"`）
- **选定**：**A 的具体形态 —— 继承 `yaml.SafeLoader` 加一个 `!include` 构造器**。
- **原因**：
  1. `yaml.unsafe_load`（aka `yaml.load` 无 Loader）认 `!!python/object` 等标签，**读一个声明文件等同于执行代码**。spec AC-10 明确禁止。
  2. `yaml.FullLoader` 在 CPython 的 PyYAML ≥ 5.1 已经不再构造任意 Python 对象，但它**不是** `safe_load` 的超集，且 `SafeLoader` 的白名单语义更清楚。CLI 既有的 `project.py` 用的是 `yaml.safe_load`（读 `bisheng-app.yaml`）——保持同一形状。
  3. **继承 `SafeLoader` 加构造器是安全的**：新构造器只做一件事——读一个文本文件并返回其内容。它不 `eval`、不查类、不导入模块。`SafeLoader` 的其余部分（不构造任意对象）原样保留。
  4. 选 A 而非 C 的理由：`!include` 是 YAML 惯用的自定义标签，语义自明；而 `@prompts/x.md` 这种前缀约定容易和真实内容撞车（提示词里本来就有 `@`）。C 的唯一优点是"零新增代码"，不值得。
- **`!include` 的边界**：
  - 路径相对**声明文件所在目录**解析（不是相对 CWD——否则换个目录跑就找不到文件）；
  - 只读文本文件，不做任何模板求值；
  - 路径逃出项目目录**不禁止**（用户自己的工作流，读自己的文件），但**不跟随符号链接去读系统文件**这条我们也不额外设防——设防等于假装这是沙箱。真正的边界是：**读文件，不是执行文件**；
  - 缺失文件 → 编译期报错，指出是哪个引用、解析成了哪个绝对路径。
- **何时该重新考虑**：若 YAML DSL 将来要支持"值来自环境变量"这类非文件来源，`!include` 会扩展成一对标签（`!include` / `!env`），届时要重新审一遍"读什么"的边界。

### 决策 8：工具注册复用后端既有解析逻辑，v2 端点只做包装

- **备选**：
  - **A. v2 端点包装既有的 `ToolServices.parse_openapi_schema` + `add_tools`**
  - **B. CLI 本地解析 OpenAPI 再提交结构**
- **选定**：**A**。
- **原因**：
  1. `tool_key` 的生成规则在后端：`tool_key = md5_hash(operationId)`（`tool/domain/services/tool.py:378`）。CLI 若本地算，就要复制这个 md5 规则和 operationId 的取名规则（`:431` 另一处 `md5_hash(one.name)`）——又是第二份。spec §3 边界情况已经写明："当工具注册使用的 OpenAPI 描述缺少操作标识时，注册应失败并说明缺少哪一项，因为操作标识是工作流引用工具的唯一依据。"判定必须在后端做。
  2. 既有端点：`POST /api/v1/tool/schema`（解析 OpenAPI → `GptsToolsTypeRead`）、`POST /api/v1/tool`（创建工具类型）、`GET /api/v1/tool`（列出，`is_preset=0` 取自定义工具）。v1 端点要 JWT，CLI 够不着（约束 2），所以要 v2 包装。
  3. **工具节点模板（AC-04）也归后端生成**：`get_tool_node_template(tool_info)` 放在后端，前端的 `getToolTree`（`util/flowUtils.ts:79-119`）与 CLI 消费**同一份生成结果**。做法是**在工具列表响应的每个 child 上加一个 `node_template` 字段**（纯增量，不改既有字段语义），前端 `getToolTree` 退化成 `children.map(c => c.node_template)`——形状逻辑全部搬走后，前端那个函数只剩三行、没有一份逻辑。
- **CLI 侧的引用方式**：YAML 里写 `tool: <operationId>`，编译时用工具列表把 operationId 解析成 `tool_key`；解析不到 → 编译期报错（spec §3 第一条边界情况）。
- **何时该重新考虑**：工具数量大到"每次编译拉全量列表太慢"时，才需要按需按 operationId 查询单个工具（`GET /api/v2/tools/{tool_key}`）。

### 决策 9：不新增任何第三方依赖

- **备选**：A. 零新增（用标准库 + 已有的 `httpx` / `PyYAML`）；B. 引入 `pydantic` 做 YAML 校验（后端有，CLI 没有）
- **选定**：**A**。
- **原因**：CLI 的依赖预算只有两个（约束 1），上游把"依赖预算"当成一条设计决策记在 `cli.py` 文件头里（"The dependency budget (design D12) buys two runtime dependencies and this is not one of them"）。YAML 解析已有 `PyYAML`；结构校验用手写规则（本 feature 的规则是"12 条固定检查"，手写反而比引入一个校验框架更直白，且能逐条给出可读报错）；HTTP 已有 `httpx`。
- **何时该重新考虑**：规则数量增长到二十条以上、或需要生成结构化错误码时。

---

## 4. 系统现状（接手必读）

### 4.1 数据流

**（a）节点模板：从后端模块到前端与 CLI**

```
workflow/common/node_templates.py（权威常量 + get_tool_node_template 生成逻辑）
  ├─→ GET /api/v1/workflow/node_templates  ──→ 前端 getWorkflowNodeTemplate()（改写前它返回本地 TS 常量）
  ├─→ GET /api/v2/flow/node_templates      ──→ CLI flow compile（取节点骨架）
  └─→ 工具列表响应的每个 child 的 node_template 字段
        ├─→ 前端 getToolTree()（改写前它在前端逐字段拼装）
        └─→ GET /api/v2/tools ──→ CLI flow compile（把 operationId 解析成 tool_key）
```

**（b）声明到画布：编译**

```
flow.yaml ──safe_load(+!include)──→ 声明树
  ──+ overlay.py（受限覆盖：只覆盖已声明参数 / 只追加未声明节点与边）──→ 声明树'
  ──+ 节点模板（v2 取，按 type 深拷贝骨架，填 params）──→ nodes
  ──+ 工具列表（v2 取，operationId → tool_key）──→ tool 节点的 group_params
  ──→ edges（source / sourceHandle / target / targetHandle / id / animated / type）
  ──→ 编译期结构校验（12 条，见 §4.4）
  ──→ {nodes, edges, viewport}
```

**（c）diff**

```
本地编译结果 {nodes, edges}
  ×  线上基准：
       默认  GET /api/v2/flows/{flow_id} → data（= 当前版本的 data 镜像）
       指定  GET /api/v2/flows/{flow_id}/versions/{version_id} → data
  ──→ 按 §3 决策 5 的比较规则 ──→ 结构化差异
```

**（d）push**

```
本地编译结果
  ──→ GET 当前版本 data ──→ diff ──→ 有线上独有内容？
                                  ├─ 是 → 交互确认 / 或要求 --allow-drop-online-only（无 TTY 时拒绝）
                                  └─ 否 → 继续
  ──→ POST /api/v2/flows/{flow_id}/versions      （建新版本，data = 本地编译结果）
  ──→ POST /api/v2/flows/{flow_id}/versions/{version_id}/activate （切当前版本）
        └─ 工作流处于上线状态 → 平台返回 10525 → push 报"版本已建，但工作流在线上，需先下线"且不报成功
```

**（e）run**

```
POST /api/v2/workflow/invoke   （workflow:invoke scope，既有端点）
  body: {workflow_id, input?, message_id?, session_id?, stream?, override?}
  → 事件流（node_run / guide_word / guide_question / input / output_msg / stream_msg / close / error）
  → 停在 input 节点：事件带 node_id 与 message_id
  → 续跑：再次 invoke，body 必须同时带 session_id 与 message_id
```

**（f）tool**

```
POST /api/v2/tools/schema  {download_url | file_content} → GptsToolsTypeRead（children 每项 name=operationId, tool_key=md5(operationId)）
POST /api/v2/tools         GptsToolsTypeRead            → 创建工具类型
GET  /api/v2/tools                                        → 列出（含 node_template）
```

### 4.2 关键数据结构 / 字段约定

**（a）工作流定义 `data`（平台既有格式，本 feature 不改）**

| 字段 | 类型 | 说明 |
|---|---|---|
| `data.nodes[]` | `{id, type:"flowNode", position:{x,y}, data:{...}}` | `type` 恒为 `"flowNode"`；真正的节点类型在 `data.type` |
| `data.nodes[].data.id` | str | 节点唯一标识，diff 的主键 |
| `data.nodes[].data.type` | str | 12 类节点类型之一（见下表） |
| `data.nodes[].data.group_params` | `[{name, params:[{key,label,type,required,value,...}]}]` | 参数按分组嵌套，**按 key 比较，不按位置** |
| `data.nodes[].data.tab` | dict | 如 `{"value":"dialog_input"}`，`input` 节点上是语义字段 |
| `data.nodes[].data.tool_key` | str | 仅 tool 节点，`md5(operationId)` |
| `data.nodes[].data.v` | int | 节点迁移版本号，**diff 排除** |
| `data.nodes[].position` | `{x,y}` | 画布坐标，**diff 排除** |
| `data.edges[]` | `{id, source, sourceHandle, target, targetHandle, animated, type}` | 语义键是 `(source, sourceHandle, target)` |
| `data.viewport` | `{x,y,zoom}` | 画布视图，**diff 排除** |

**（b）12 类节点类型**（`workflow/common/node.py` 的 `NodeType`）

`start` / `end` / `input` / `output` / `llm` / `agent` / `qa_retriever` / `rag` / `knowledge_retriever` / `report` / `code` / `condition`

（`NodeType` 里还有 `fake_output` 与 `note` 两个值。`fake_output` 是 `output` 节点在图编译时自动生成的伴生节点，**不由用户声明**；`note` 是画布批注，运行时被跳过（`graph_engine.py` 的 `init_nodes` 里 `if node_data.type == NodeType.NOTE.value: continue`）。两者都不进 DSL。）

**（c）变量引用语法**

格式 `{{#node_id.var#}}`，正则（`workflow/nodes/prompt_template.py:3-5`）：

```
\{\{#([#a-zA-Z_][a-zA-Z0-9_]{0,29}|[#a-zA-Z0-9_]{1,50}\.[#a-zA-Z0-9_\.]{1,100})#\}\}
```

~~另有三个特例键：`{{#histories#}}`、`{{#query#}}`、`{{#context#}}`。~~ **（2026-11-21 实测更正）** 这句来自 `prompt_template.py` 的文档字符串，但**工作流引擎里没有任何节点特殊处理它们**：所有变量都经 `GraphState.get_variable_by_str` 解析，它无条件按 `.` 切分——单键引用运行时抛 `IndexError`。所以工作流里的引用只有 `<节点>.<变量>` 一种形式（校验 #8 据此报 `reference_without_node`）。

**⚠ 不匹配的引用在运行时是静默不替换的**（`PromptTemplateParser.format` 的 `replacer` 里 `inputs.get(key, match.group(0))`——找不到就原样返回）。所以 AC-16 的编译期检查是硬需求。

**（d）条件节点的分支句柄**

`condition` 节点的 `group_params` 里 `condition` 数组的每一项是一个 case，**case 的 `id` 就是它那条出边的 `sourceHandle`**；运行时第一个为真的 case 胜出（`nodes/condition/condition.py:22-28`）；全不匹配走 `right_handle`（`:31-32`）。

比较操作符 13 个（`workflow/common/condition.py` 的 `ComparisonType`）：`equals` / `not_equals` / `greater_than` / `less_than` / `greater_than_or_equal` / `less_than_or_equal` / `contains` / `not_contains` / `is_empty` / `is_not_empty` / `starts_with` / `ends_with` / `regex`；逻辑只有 `and` / `or`（`LogicType`）。

**（e）工具节点**

参数**全是字符串**；空值被丢弃；模板化每个参数；固定返回 `{"output": ...}`（`nodes/tool/tool.py` 的 `parse_tool_input`）。`tool_key` 由 `operationId` 派生。

**（f）版本与当前版本**

`Flow.data` 是**当前版本 data 的镜像**。`FlowVersionDao.change_current_version`（`database/models/flow_version.py:250-278`）把旧版本的 `is_current` 置 0、目标置 1，并把 `parent.data = target.data`。**没有任何版本的 `data` 被改写。**

`FlowDao.create_flow`（`database/models/flow.py:174-210`）在创建工作流时顺带建一个 `name="v0"`、`is_current=1` 的隐式版本，其 `data` 就是建流时的 `data`。

### 4.3 关键模块职责

| 模块 / 文件 | 职责 | 不做什么 |
|---|---|---|
| `workflow/common/node_templates.py`（**新增**） | 12 类节点的模板常量（从前端 TS 字面量逐字搬来）+ `get_tool_node_template(tool_info)` 生成工具节点模板 | 不含界面文案（文案在前端 `public/locales/*/flow.json`） |
| `workflow/common/node.py` | `NodeType` 枚举、`BaseNodeData` / `NodeParams` / `NodeGroupParams` 数据形状 | 不含模板 |
| `api/v1/workflow.py` | v1 工作流路由（既有） | 新增 `GET /node_templates` 之外不改既有端点 |
| `open_endpoints/api/endpoints/flow.py` | v2 `GET /flows/{flow_id}`（既有） | 不做写 |
| `open_endpoints/api/endpoints/workflow_write.py`（**新增**） | v2 工作流写面 + 工具注册面 | **不自行解析身份**（约束 4）；不新写授权逻辑（复用 v1 service） |
| `open_endpoints/domain/utils.py` | `get_open_api_operator[_async]()`：principal → `UserPayload` 的桥 | 不做业务判定 |
| `api/services/flow.py` | `WorkFlowService`：既有 v1 业务服务（`judge_flow_write_permission` / `create_new_version` / `change_current_version` / `update_version_info`） | 不被本 feature 修改 |
| `open_api/domain/scopes.py` | scope 注册表 + `@open_api_scope` 装饰器 | 不含端点实现 |
| `tool/domain/services/tool.py` | `parse_openapi_schema`（OpenAPI → 工具类型）、`tool_key` 生成 | 不被本 feature 修改 |
| **CLI** `bisheng_cli/flow/spec.py`（**新增**） | YAML 加载（`SafeLoader` + `!include`）、声明树的数据形状 | 不做权限判定；不填默认值 |
| **CLI** `bisheng_cli/flow/overlay.py`（**新增**） | 受限 Python 覆盖层：只覆盖已声明参数 / 只追加未声明节点与边 | 不能删改已声明结构 |
| **CLI** `bisheng_cli/flow/compiler.py`（**新增**） | 声明树 → `{nodes, edges, viewport}` | 不发请求（除取模板与工具列表） |
| **CLI** `bisheng_cli/flow/validate.py`（**新增**） | 编译期 12 条结构校验 | 不裁定业务语义 |
| **CLI** `bisheng_cli/flow/diff.py`（**新增**） | 结构化差异算法 | 不做权限判定 |
| **CLI** `bisheng_cli/flow/client.py`（**新增**） | v2 调用封装（模板、工具、版本读写、activate、invoke） | 不解析凭据（用 `credentials.py`） |
| **CLI** `bisheng_cli/commands/flow.py`（**新增**） | `flow` 子命令族分发（compile / diff / push / run / tool） | 不含算法 |
| **前端** `controllers/API/workflow.ts` | `getWorkflowNodeTemplate()` 改为请求后端 | 删除本地 `workflowTemplate` 字面量（约 890 行） |
| **前端** `util/flowUtils.ts` | `getToolTree()` 退化为 `children.map(c => c.node_template)` | 不再拼装形状 |

### 4.4 编译期结构校验清单（12 条）

后端**不**做节点级校验（`Flow.data` 只校验"是 dict 且含 `nodes`/`edges` 两个 key"），这些校验是在补**服务端运行时才暴露**的那一层。每条都给出**可定位的报错**（哪个节点、哪个参数、哪个引用）。

| # | 校验 | 依据 |
|---|---|---|
| 1 | 至少一个节点 | `graph_engine.py` 的 `build_nodes`（"workflow must have at least one node"） |
| 2 | 存在 `start` 节点 | 同上（"workflow must have start node"） |
| 3 | 节点 `id` 非空且唯一 | `graph_engine.py` 的 `init_nodes`（"node must have attribute id"）+ 重复 id 会让 `nodes_map` 覆盖 |
| 4 | 节点 `type` 在 12 类之内 | `workflow/nodes/node_manage.py:16-30` 的 `NODE_CLASS_MAP`（不在表内 → `instance_node` 抛 "Unknown node type"） |
| 5 | 非 `end` 节点至少一条边（**进或出均可**） | `graph_engine.py` 的 `add_node_edge`（`if not target_node_ids and not source_node_ids` → "must have at least one edge"） |
| 6 | 每条边的 `target` 节点存在 | `graph_engine.py`（"target node {id} not found"） |
| 7 | **条件节点**出边的 `sourceHandle` 是它的合法出口（某个 case id，或 `right_handle`） | `nodes/condition/condition.py:26`（`get_next_node_id(one.id)`）与 `:32`（`right_handle`）。只有 `condition` 与 `output`（choose 模式，按选项键分支）读句柄，其余节点运行时忽略句柄——所以只判条件节点，`output` 的选项键不是这里的封闭集合 |
| 8 | 变量引用符合 `{{#...#}}` 格式且引用到的节点存在（含条件节点的 `left_var` 与 `ref` 型右值） | `workflow/nodes/prompt_template.py:3-5`；**不匹配则运行时静默不替换**。"格式错"指同一行内有收尾 `}}` 的 `{{#…}}`；孤立的 `{{#`（如提示词里提醒模型别写这个标记）是正文，引擎也不动它，不报 |
| 9 | 节点声明了**该类型必填的参数**（见下方"按节点类型的必填参数表"） | 各节点类的 `self.node_params["..."]` 下标访问（下标 = 必填，`.get` = 可选） |
| 10 | `llm` / `agent` 的 `batch_variable` 与 `output[i].key` 一一对应 | `nodes/llm/llm.py:58-60`（`self.node_params['output'][index]['key']`）、`nodes/agent/agent.py:552` |
| 11 | `tool` 节点的 `tool_key` 能在已注册工具里解析到 | `nodes/tool/tool.py` 的 `tool_key` 查找 |
| 12 | `condition` 节点有 `condition` 数组，且每个 case 有 `id`，每个条件有 `left_var` / `comparison_operation`（13 个比较符之一）/ `right_value_type` / `right_value` | `nodes/condition/condition.py:14`（`self.node_params['condition']`）与 `ConditionOne`（`nodes/condition/conidition_case.py`，四个字段全是 `Field(...)` = 必填）。**case 的 `operator`（不是 `logic`）与 `conditions` 都是可选**——`ConditionCases.operator` 默认 `and`，`conditions` 为空的 case 恒为真，就是平台的 else 分支；把它们当必填会误报合法定义（AC-18）。（2026-11-21 实现时对照引擎修正） |

**按节点类型的必填参数表**（校验 #9 的依据；`.get` 的是可选、不查）：

| 节点类型 | 必填参数（下标访问） | 可选（`.get`） | 依据 |
|---|---|---|---|
| `start` | —（模板自带 `current_time` 等默认值） | `guide_word` / `guide_question` / `preset_question` | `nodes/start/start.py` |
| `end` | — | — | `nodes/end/end.py` |
| `input` | `user_input` | `dialog_files_content` | `nodes/input/input.py:59-62` |
| `output` | `output_result`（含 `type` / `value`）、`output_msg` 或 `message`（二选一） | — | `nodes/output/output.py:22-33` |
| `llm` | `system_prompt` / `user_prompt` / `model_id` / `batch_variable` / `output` | `output_user` | `nodes/llm/llm.py:28-60` |
| `agent` | `system_prompt` / `user_prompt` / `model_id` / `chat_history_flag` / `tool_list` / `knowledge_id` / `batch_variable` | `user_auth` / `output_user` / `sql_agent`（先 `.get` 取到且 `open` 为真才下标访问——键缺失不会崩，**不列为必填**） | `nodes/agent/agent.py:268-314`、`:552` |
| `code` | `code_input` / `code` / `code_output` | — | `nodes/code/code.py:33-35` |
| `qa_retriever` | `user_question` | — | `workflow/common/knowledge.py:313`（`init_user_question`） |
| `rag` | `system_prompt` / `user_prompt` / `model_id` | — | `nodes/rag/rag.py:40-48` |
| `knowledge_retriever` | `user_question` | — | 同 `qa_retriever` |
| `report` | `report_info` | — | `nodes/report/report.py:18` |
| `tool` | `tool_key`（在 `data` 层，不在 `group_params`） | 各工具参数（空值被丢弃） | `nodes/tool/tool.py` |
| `condition` | `condition`（数组，每项见校验 #12） | — | `nodes/condition/condition.py:14` |

**这张表是"编译器补的是服务端运行时才暴露的那一层"的清单本体**——它逐条对应后端某处的一次下标访问。后端加一种节点类型，这张表必须同批更新（与 AC-44 的"同一次变更"是同一件事）。

**校验的判定边界**：以上 12 条**只在编译结果确定无疑时不报**。像"某个参数在平台上不参与执行"这类判断**不做**（spec §3 边界情况明确：差异工具不替平台判断）。

**（重要）每条规则都要有"该报的报出来"和"不该报的不误报"两条用例。** 只写正例的测试等于没测——这正是 invest 侧"守卫工具自己也会失效"的教训（守卫的调用点与输入也能让它自己失效）。

### 4.5 spec AC 的落点索引

接手的人想知道"这条验收标准在设计里对应哪一块"时查这张表。

| AC | 落点 |
|---|---|
| AC-01 | 决策 2；§4.3（`workflow/common/node_templates.py`）；§6.1 |
| AC-02 | 决策 2；§4.1(a)；§6.1 的"破坏性变更清单"（前端两个消费点） |
| AC-03 | 决策 2（模板只发结构与参数 key）；§5 坑 10、坑 12 |
| AC-04 | 决策 8；§4.3（`get_tool_node_template`）；§6.1（`node_template` 字段） |
| AC-05 | 决策 2（**不得回落硬编码副本**）；§4.1(a) 的缓存说明 |
| AC-06 | 决策 1；§4.1(b) |
| AC-07 | 决策 7（`!include`）；§6.5 |
| AC-08 | 决策 6（覆盖层语义表） |
| AC-09 | 决策 1、决策 6 |
| AC-10 | 决策 7（`SafeLoader` 子类，拒绝 `unsafe_load`） |
| AC-11 | 决策 1（编译在本地）；§6.5 |
| AC-12 ~ AC-18 | §4.4 的 12 条校验（含按节点类型的必填参数表） |
| AC-19 ~ AC-24 | 决策 5（比较规则表）；§4.1(c) |
| AC-25 | 决策 4（push = 建 + 切） |
| AC-26 | 决策 4（线上独有内容的处置；无 TTY 拒绝 + `--allow-drop-online-only`） |
| AC-27 | 决策 4（10525 的落点） |
| AC-28 | §6.3（错误码 → 退出码映射；缺 scope 时平台返 26003，CLI 已有中文提示） |
| AC-29 | 决策 4（push 报版本 id 与名称） |
| AC-30 ~ AC-33 | §4.1(e)（run 的数据流与续跑协议） |
| AC-34 ~ AC-37 | 决策 8；§4.1(f) |
| AC-38 | 决策 3（`workflow:write` 是专用位，无其他位可替代） |
| AC-39 | §2 的 Constitution Check（C3 / C4）；决策 3 |
| AC-40 | 决策 3（授权不可判定 → 拒绝；`get_open_api_operator` 对归属人缺失抛错） |
| AC-41 | §2 约束 3（掩码挂进程出口）；§7.4 |
| AC-42 | 决策 3（`modes=("S",)`，不给"指定他人身份"的入口） |
| AC-43 | 决策 3（归属人缺失显式拒绝；§5 坑 9） |
| AC-44 | 决策 2 的"AC-44 在本决策下的具体兑现"；§4.4 末尾（必填参数表与后端同批更新） |

---

## 5. 已知坑 / 反直觉事实

| # | 反直觉事实 | 如果不知道会怎样 | 在哪处理 |
|---|---|---|---|
| 1 | **`Flow.data` 是当前版本 data 的镜像，不是"工作流的定义"**。`change_current_version` 里 `parent.data = target.data` | 只建版本不切当前版本 → 推送"成功"但跑的还是旧定义，diff 永远显示有差异 | 决策 4；`flow push` 默认建+切 |
| 2 | **平台对不认识的变量引用是静默不替换的**（`PromptTemplateParser.format` 找不到 key 就原样返回 `{{#x.y#}}`） | 拼错的引用进了线上，提示词里出现一串 `{{#...#}}` 字面量，且**没有任何报错** | 校验 #8；AC-16 |
| 3 | **后端几乎没有节点级校验**：`Flow.data` 只查"是 dict 且含 nodes/edges" | 以为"推上去平台会验"，结果结构错误只在**运行时**炸 | 校验 12 条 |
| 4 | **`PATCH /api/v1/workflow/update/{flow_id}` 显式跳过 `data`**（`api/v1/workflow.py` 里那几行 `continue`） | 用它"更新画布"会静默什么也没改 | 本 feature 不用该端点；写路径只走版本端点 |
| 5 | **改画布的唯一入口是 `PUT /api/v1/workflow/versions/{version_id}`**，且当前版本 + 上线状态 + 带 data 三个条件同时成立时被拒（10525） | 以为能直接改当前版本 | `flow push` 走"建新版本 + 切" |
| 6 | **服务的账号密钥（`bs-sak-*`）只能打 `/api/v2`**；v1 要 JWT，CLI 解不了 | 给 CLI 设计 v1 端点 → 本地怎么都跑不通 | 约束 2；决策 3 的两个端点 |
| 7 | **`open_api_scope` 装饰器在 scope 未注册时于 import 期抛 `ValueError`**（`scopes.py:280`） | 先加端点后加 scope → 后端整个起不来 | 决策 3；scope 注册表必须先改 |
| 8 | **`get_open_api_operator()` 对服务账号强制清空角色**（`user_role=[]`、`is_global_super=False`） | 以为服务账号走的是"管理员"路径 → 权限判定结果与网页端不一致 | 决策 3；直接复用这个桥，别自己造 `UserPayload` |
| 9 | **归属人缺失必须显式拒绝，不能 `or 0` 兜底**。`resource_owner_of` 的注释写明了原因："没归属人"退化成 owner `0` 会让所有权比对放进任何无主行 | 用 `principal.resource_owner_user_id or 0` 写捷径 → 无主工作流变成谁都能改 | 决策 3；复用 `get_open_api_operator`，它已经拒绝 |
| 10 | **前端节点模板那份 TS 字面量约 890 行**（`controllers/API/workflow.ts:185-1077`），且每个节点的 `label`/`description` 都是 `"true"` 占位符，真实文案在前端 `public/locales/*/flow.json`（13 类型共 149 个 key） | 把文案搬进后端 → 后端引入界面语言依赖（AC-03 禁止） | 决策 2；模板只发结构与参数 key |
| 11 | **`getToolTree` 是前端 `util/flowUtils.ts:79-119` 的一整段形状拼装**；invest 侧 `tools/bisheng/bsflow.py` 的 `tool_tpl` 是它的**逐行 Python 复刻** | 只搬节点模板不搬工具模板 → 漂移换个地方继续 | 决策 8；`get_tool_node_template` 归后端 |
| 12 | **前端 `initNode`（`util/flowUtils.ts:29-77`）只对 `var_textarea` 类型的参数做 i18n 替换** | 把所有 `label` 都当占位符处理 → 改坏非 textarea 参数的显示 | 决策 2；模板接口不碰文案 |
| 13 | **`open_api_scope` 的 `endpoints` 元组是声明性的**，实际强制在端点装饰器上 | 只改元组不加装饰器 → 端点没有标记 → fail closed（403 / `OpenApiEndpointUnregisteredError`） | 决策 3；两处同步改 |
| 14 | **`condition` 的 case `id` 就是出边的 `sourceHandle`**，不是随便起的名字 | 把 case id 当注释用 → 出边接不上，条件节点永远走 `right_handle` | 校验 #7；DSL 里 `handle` 直接写 case id |
| 15 | **`tool` 节点参数全是字符串，空值被丢弃** | 在 YAML 里写数字 / 列表 → 运行时被丢掉或报错 | DSL 侧把工具参数一律当字符串 |
| 16 | **本机毕昇容器跑的是打进镜像的代码，不 bind mount 源码** | 改完源码在本机"试一下"——跑的还是旧代码 | 端到端验证前必须重建镜像（§7） |
| 17 | **CLI 的 `confirm()` 在无 TTY 时抛错而不是默认同意** | 照抄别的 CLI 的"无 TTY 默认 yes" → 无人值守时静默覆盖别人的东西 | 决策 4；沿用 `cli.py` 的 `confirm()` |
| 18 | **`Flow.data` 与 `FlowVersion.data` 在建流时同源，但之后各自演化**——切版本才同步 | 同时写两处 → 两边不一致，diff 与运行结果对不上 | 本 feature 只经版本端点写（决策 3/4） |
| 19 | **上游 CLI 的依赖预算是两条**（`httpx` + `PyYAML`），并把"不加第三个"当成一条设计决策写在文件头 | 图省事加 `pydantic` / `jsonschema` → 与上游形态分家 | 决策 9 |
| 20 | **`prompt_template.py` 的正则里 node_id 段最长 30 字符、单变量键最长 30；带点的形式则是 node 段 ≤50、变量段 ≤100** | 用超长 id 或带空格的名字 → 引用不匹配 → 静默不替换（回到坑 2） | 校验 #8 同时查长度 |
| 21 | **YAML 里写的 `model_id` / `knowledge` / `report_info.version_key` 都是平台侧的资源标识，声明格式本身无法校验它们存在**。编译期最多能查"这个键填了没有"，查不了"这个 id 还有效吗" | 声明里写了个已下线的模型 id → 编译通过、push 成功、**跑到那个节点才炸**；而 diff 会显示"本地与线上一致"，看起来一切正常 | 编译期只报"缺参数"不报"资源无效"；资源有效性归运行时。`report` 节点另有一层：模板在对象存储上，导入时要换新 key（§8 短板） |
| 22 | **`output` 节点的出参有两种互斥形状**：`output_msg`（新）与 `message`（旧）。后端按"`output_msg` 在就取它，否则取 `message`"兼容 | 只写 `output_msg` 的 YAML 被套上旧模板（或反之）→ 节点报 KeyError | 校验 #9 的必填表把两者都列为"二选一" |
| 23 | **`agent` 节点的 `sql_agent` 是"条件必填"**：只有它的 `open` 为真时才会被下标访问 | 按"必填参数"一刀切地检查 → 把 `open: false` 的正常 agent 报成缺参数 | 校验 #9 的必填表把 `sql_agent` 单列为条件必填 |

---

## 6. 对外契约与依赖

### 6.1 我提供给别人的（Outgoing）

| 契约 | 形式 | 谁在用 |
|---|---|---|
| `GET /api/v1/workflow/node_templates` | HTTP API（v1，JWT） | platform 前端（节点库、知识库问答助手快捷建流） |
| `GET /api/v2/flow/node_templates` | HTTP API（v2，`workflow:read`） | CLI `flow compile` |
| `GET /api/v2/flows/{flow_id}/versions` | HTTP API（v2，`workflow:read`） | CLI `flow diff --list-versions` |
| `GET /api/v2/flows/{flow_id}/versions/{version_id}` | HTTP API（v2，`workflow:read`） | CLI `flow diff --version-id` |
| `POST /api/v2/flows` | HTTP API（v2，`workflow:write`） | CLI `flow push --create` |
| `POST /api/v2/flows/{flow_id}/versions` | HTTP API（v2，`workflow:write`） | CLI `flow push` |
| `PUT /api/v2/flows/{flow_id}/versions/{version_id}` | HTTP API（v2，`workflow:write`） | CLI（备用：只改元信息 / 补丁式更新） |
| `POST /api/v2/flows/{flow_id}/versions/{version_id}/activate` | HTTP API（v2，`workflow:write`） | CLI `flow push` |
| `GET /api/v2/tools` | HTTP API（v2，`workflow:read`） | CLI `flow tool list` / `flow compile` |
| `POST /api/v2/tools` | HTTP API（v2，`workflow:write`） | CLI `flow tool register` |
| `POST /api/v2/tools/schema` | HTTP API（v2，`workflow:write`） | CLI `flow tool register` |
| 工具列表响应中每个 child 的 `node_template` 字段 | 响应体增量字段 | 前端 `getToolTree`、CLI `flow compile` |
| `bisheng flow {compile,diff,push,run,tool}` | CLI 命令 | 本地 agent、开发者 |
| `.bisheng/flow.json`（项目内本地状态，形状照 `.bisheng/app.json`） | 文件 | CLI 自身；进 git；结构上排除出上传包 |
| `bisheng.workflow.common.node_templates.NODE_TEMPLATES` | 内部 Python API（12 类节点模板的权威常量） | 后端自身的测试、前端契约对照 |
| `bisheng.workflow.common.node_templates.get_tool_node_template(tool_info)` | 内部 Python API（由工具信息生成工具节点模板） | v1 / v2 工具列表端点 |

**破坏性变更清单**（改了会坏掉谁）：

- 删除前端 `workflowTemplate` 常量 → 前端任何**直接 import** 它的地方都会编译失败。已知消费者只有 `getWorkflowNodeTemplate`（改造后即不再引用），但 `workflow.ts` 是 1077 行的大文件，改动前用 grep 全量确认（`grep -rn "workflowTemplate" src/`）。
- `getToolTree` 的**返回形状不变**（仍是 `{name, is_preset, children}`），只是 `children` 的元素从"前端拼的"变成"后端给的"。前端 `Sidebar.tsx` 的消费方式不变。
- 工具列表响应**新增** `node_template` 字段（增量，不动既有字段）→ 严格消费既有字段的第三方不受影响。
- 新增 scope `workflow:write` → 已在签发的密钥**不受影响**（scope 是签发时固化的），但签发表单会多出一个可选项。

### 6.2 我依赖别人的（Incoming）

| 依赖 | 形式 | 风险点 |
|---|---|---|
| `Flow.data` 是当前版本 data 的镜像 | 隐式数据契约 | 平台若改为"Flow.data 不再镜像"，diff 的默认基准与 push 的语义都要重写（§5 坑 1、坑 18） |
| `FlowVersionDao.change_current_version` 把目标版本 data 复制进 `Flow.data` | 隐式数据契约 | 若平台改为"Flow.data 只在显式同步时更新"，`flow push` 的 activate 步骤需要额外同步调用 |
| `PromptTemplateParser` 的正则 | 隐式数据契约 | 平台若放宽/收紧变量引用格式，编译期校验 #8 会误报或漏报 |
| `NODE_CLASS_MAP` 的 12 类节点类型 | 隐式数据契约 | 平台新增节点类型时，节点模板与校验 #4 都要同批更新（AC-44 的"同一次变更"在这里第一次真正生效） |
| `tool_key = md5(operationId)` | 隐式数据契约 | 平台改派生规则 → 已有工作流的 `tool_key` 全废；本 feature 只做透传，不重算 |
| `compare_openapi_schema` 产出的 `GptsToolsTypeRead` 形状 | HTTP 契约 | 平台改字段 → `flow tool register` 与 `node_template` 生成都要跟着改 |
| `get_open_api_operator[_async]()` 的语义 | 内部 Python API | 它若改了"服务账号如何取身份"，本 feature 的授权路径整体要重走一遍（**这是本 feature 最大的单点依赖**） |
| `WorkFlowService.create_new_version` / `change_current_version` / `update_version_info` 的签名与返回 | 内部 Python API | v2 端点是薄包装；签名一变，包装层要跟着改（但业务语义不变） |
| 105xx 错误码段的分配 | 错误码注册表 | 新增码要避开既有 105xx（见 §6.3） |
| CLI 凭据存储 `~/.bisheng/credentials.json`（0600，明文 key） | 本地文件 | 明文是**有意为之**——文件模式就是保护；不要"顺手加密" |
| **对象存储（MinIO）**：report 节点的报告模板（`report_info.version_key` 指向的 docx）、output 节点产出的文件 | 平台服务 | report 节点要"复制模板换新 key"才能被另一条工作流引用（§8 短板）；输出节点的文件在对象存储上 |
| **毕昇的模型服务**：`model_id` 指向的平台内 LLM | 平台服务 | 模型被下架 / 额度耗尽 → 声明仍能编译、push 仍能成功，运行时才失败（§5 坑 21） |
| **LLM 供应商**（毕昇背后真正调用的外部模型服务） | 第三方服务 | 工作流运行期依赖；本 feature 不碰，但 `flow run` 的失败原因里可能混着供应商错误（AC-32 要求报出"失败节点与失败原因"） |
| **Python 运行环境**：`code` 节点执行用户代码、`agent` 的 SQL 能力 | 平台运行时 | `code` 节点的代码在毕昇的执行环境里跑；声明格式不改变它的安全边界（不在本 feature 范围） |

### 6.3 错误码与退出码

**后端错误码**（`common/errcode/flow.py` 的 105 段已占用）：10500 / 10501 / 10502 / 10525 / 10526 / 10527 / 10528 / 10529 / 10530 / 10531 / 10532 / 10540 / 10541 / 10542 / 10543 / 10550 / 10599。

本 feature **不新增 105xx 码**：既有码已经覆盖了要报的场景（10525 上线不可编辑、10529 工作流重名、10502 版本名重复、10500 版本不存在、10543 输入与等待消息不匹配）。**新增码只在"既有码都不表达这个意思"时才做**，且必须落在 105 段内、避开上表。

**CLI 退出码映射**（`bisheng_cli/errors.py` 的 `ERROR_EXIT_CODES` 目前只映射 16xxx / 26xxx，**没有 105xx**）：

| 后端码 | CLI 退出码 | 语义 |
|---|---|---|
| 10525 | `EXIT_PRECHECK_FAILED`（10） | 工作流在线上，需先下线 |
| 10529 / 10502 | `EXIT_PUBLISH_CONFLICT`（12） | 名字冲突（工作流 / 版本） |
| 10500 | `EXIT_LOCAL_INVALID`（6） | 版本不存在 |
| 10543 | `EXIT_LOCAL_INVALID`（6） | 续跑时的会话 / 消息不匹配 |
| 10599 | `EXIT_FORBIDDEN`（5） | 无写权限 |
| 权限不足（平台 403 / 缺 scope） | `EXIT_FORBIDDEN`（5） | — |

**权限位缺失（AC-28）** 的报错必须**指出缺哪个权限位**——平台对缺 scope 返回 26003（`"这把密钥缺少本次操作需要的权限位"`），CLI 已有该码的 `ERROR_HINTS`（`errors.py` 里 26003 那条），会直接翻成人话。

### 6.4 CLI 命令面

```
bisheng flow compile <spec.yaml> [--overlay overlay.py] [--json] [--out FILE]
bisheng flow diff    <spec.yaml> [--flow-id ID | --version-id ID] [--json]
bisheng flow push    <spec.yaml> [--flow-id ID] [--name NAME] [--allow-drop-online-only]
                            [--no-activate] [--create] [--json]
bisheng flow run     <spec.yaml> [--input TEXT | --input-file FILE] [--json]
bisheng flow tool    register <openapi.yaml> [--json]
bisheng flow tool    list [--json]
```

- `--flow-id` 缺省时从项目内 `.bisheng/flow.json` 读（形状照 `project.py` 的 `.bisheng/app.json`）。
- **全局 flag 必须镜像到每个叶子**（`cli.py` 的 `_add_global_flags(parser, mirror=True)`，用 `default=argparse.SUPPRESS`）。`flow` 是**两 token 命令**（`flow` + 动词），照 `skills` / `platforms` 的形状：`flow` 自己的 subparser 携带动词，每个动词叶子再镜像一遍全局 flag——`bisheng flow push --json` 与 `bisheng --json flow push` 两种顺序都要能跑。
- **每条退出路径恰好一个 `result` 事件**（`main.py` 的不变量）。
- `--json` 时 NDJSON 走 stdout、人读文本走 stderr（`output.py` 的既定分工）；最后一行恒为 `result` 事件。

### 6.5 YAML DSL 表面（本 feature 定义的键名）

```yaml
name: <工作流名>
description: <描述>

nodes:
  - id: <节点 id>
    type: <12 类之一>
    name: <显示名>                  # 可选，缺省 = id
    description: <说明>             # 可选
    position: {x: 0, y: 0}         # 可选，缺省按序号排布（diff 不比较画布坐标）
    params:
      <param_key>: <值>          # 键必须是该节点模板里存在的参数键
    tab: <字符串>                  # 可选，仅 input 节点等少数类型有意义
  - id: <tool 节点 id>
    type: tool
    tool: <operationId>           # 按操作标识引用已注册工具；多个工具类型同名时写 <工具类型名>/<operationId>
    params:
      <param_name>: <字符串>       # 工具参数一律字符串

  - id: <condition 节点 id>
    type: condition
    cases:
      - id: <case id>              # 这个 id 就是出边的 sourceHandle
        logic: and | or            # 可选，缺省 and（编译为平台的 operator）
        conditions:                # 可选；不写 = else 分支（恒为真）
          - id: <条件 id>           # 可选；缺省按 <case id>_c<序号> 生成（已有线上流写原 id，免得 diff 报差异）
            left: <变量引用>        # 如 input_idea.user_input
            op: <13 个比较符之一>
            value: <字面量>          # 与 ref 二选一；right_value_type = input
            ref: <变量引用>          # 与 value 二选一；right_value_type = ref
            label: <显示名>          # 可选；缺省按画布约定生成 <节点显示名>/<变量名>（仅显示）

edges:
  - from: <节点 id>
    to: <节点 id>
    handle: <case id | right_handle>   # 可选，缺省 = right_handle
```

- `!include <相对路径>`：在任意字符串值处展开一个文本文件（决策 7）。
- 变量引用：`{{#node_id.var#}}`（§4.2(c)）。
- **YAML 里的键名只有这一处定义**（CLI 的 `flow/spec.py`）；后端不读 YAML（决策 3 的"触发条件"）。
- 节点与连线上**不认识的键一律编译报错**（如把 `params` 拼成 `parms`），不静默丢弃它携带的值。
- 顶层必填只有 `name` / `nodes`；`edges` 缺失由校验 #5 按节点逐个指出（比"缺 edges"更可定位）。

---

## 7. 测试与可观测

### 7.1 分层策略

| 层 | 覆盖什么 | 关键点 |
|---|---|---|
| 后端单测 | 节点模板模块的形状完整性（12 类齐全、每类参数键与前端模板逐字一致）；`get_tool_node_template` 与前端 `getToolTree` 原先的产出逐字段相等 | 用**前端那份 TS 字面量作为黄金参照**做一次性对照，对照通过后把黄金固化成后端测试的期望值 |
| 后端单测 | v2 端点的 scope 标记齐备（每个端点都有 `@open_api_scope`）；scope 注册表里有对应条目 | `open_api_scope` 未注册会 import 期抛错——注册表与端点必须同时改，这条测试是"改了 scope 忘了加端点"的哨兵 |
| 后端集成测 | 用 `get_open_api_operator_async` 造 principal → `require_business_action(action="edit")` 的判定结果；服务账号（mode S）拿到的是**非管理员**身份 | **必须有一组反向用例**：归属人缺失 → 拒绝；`hosted_app` → 拒绝；非归属人 → 拒绝 |
| CLI 单测 | YAML 加载（**`!!python/object` 载荷必须被拒**——照 `project.py` 的做法）；`!include` 展开；缺顶层字段 → 本地拒绝且**不发任何请求** | `no_network` 哨兵会抓住"本地校验却发了请求" |
| CLI 单测 | 编译 12 类节点产出完整骨架；工具节点解析 `tool_key`；`!include` 的提示词展开 | |
| CLI 单测 | **编译期校验 12 条，每条两条用例**（该报的报出来 / 不该报的不误报） | 只写正例的测试等于没测（§4.4 末尾） |
| CLI 单测 | **diff 双向探针**：线上多节点 / 少节点 / 改参数值 / 加边 / 删边 → 必须报出来；**仅 `position` 不同 → 不算差异**；仅 `viewport` 不同 → 不算差异；仅 `data.v` 不同 → 不算差异；仅 `label` 不同 → 不算差异；`tab.value` 不同 → **算差异** | 对照用例是必须的 |
| CLI 单测 | 覆盖层：只覆盖已声明参数 / 只追加未声明节点与边 → 允许；删节点 / 删边 / 改已声明边的 handle / 改已声明节点 id → **拒绝并逐项列出被拒项** | |
| CLI 单测 | 无 TTY 时 `flow push` 遇线上独有内容 → 拒绝并要求 `--allow-drop-online-only`（**不默认同意**） | 照 `cli.py` 的 `confirm()` 语义 |
| CLI 契约测 | 已公告的命令都真的注册了（`announced ≠ registered`）；每个叶子都镜像了全局 flag；每条退出路径恰好一个 `result` 事件 | 照上游既有测试形状 |
| 端到端（`network` marker） | 真平台上 `compile → diff → push → run` | `BISHENG_CLI_RUN_NETWORK_TESTS=1` 才跑（默认跳过） |

### 7.2 手动验证（本机毕昇）

> **前置**：本机毕昇容器跑的是**打进镜像的代码**，不 bind mount 源码（§5 坑 16）。**改完后端代码必须先重建镜像**，否则验的是旧代码。
>
> **两种凭据从哪来**：
> - `<JWT>` —— 平台网页登录后的用户态令牌（v1 面用）。在网页登录一次，从浏览器请求头 `Cookie: access_token_cookie=...` 里取。
> - `bs-sak-...` —— 服务账号密钥（v2 面用）。在平台的**服务账号详情页**签发，并勾上本 feature 需要的权限位（`workflow:read` + `workflow:write`；跑工作流另需 `workflow:invoke`）。CLI 的 `bisheng login <base_url> --api-key-stdin` 存的就是它。

```bash
# 0. 重建镜像并重启（改了后端代码后必做）
cd C:/Dev/projects/bisheng-rs/bisheng/docker && docker compose up -d --build

# 1. 节点模板接口（前端路径，v1 + JWT）
curl -s http://127.0.0.1:7860/api/v1/workflow/node_templates \
  -H "Cookie: access_token_cookie=<JWT>" | jq '.data | length'      # 应为 12

# 2. v2 模板接口（CLI 路径，需 bs-sak-*）
curl -s http://127.0.0.1:7860/api/v2/flow/node_templates \
  -H "Authorization: Bearer bs-sak-..." | jq '.data | length'          # 应为 12

# 3. CLI 编译（不发写请求）
cd <flow-project> && bisheng flow compile flow.yaml --json | jq '.nodes | length'

# 4. diff（对线上当前版本）
bisheng flow diff flow.yaml --json | jq '.details'

# 5. 推送（建新版本 + 切当前版本）
bisheng flow push flow.yaml --json | jq '.details'

# 6. 跑（需 workflow:invoke）
bisheng flow run flow.yaml --input "研究想法" --json

# 7. 端到端
cd src/bisheng-cli && BISHENG_CLI_RUN_NETWORK_TESTS=1 uv run pytest tests/ -m network -v
```

### 7.3 回归闸门

| 闸门 | 命令 |
|---|---|
| 后端测试 | `cd src/backend && uv run pytest test/workflow/ test/open_api/ -v` |
| 后端 lint | `cd src/backend && uv run ruff check bisheng/workflow/ bisheng/open_endpoints/` |
| 架构守卫 | `bash scripts/arch-guard.sh <每个改动的 .py>` |
| CLI 测试 | `cd src/bisheng-cli && uv run pytest tests/ -v` |
| CLI lint | `cd src/bisheng-cli && uv run ruff check bisheng_cli/` |
| 前端 | `cd src/frontend && pnpm lint && pnpm typecheck` |

**行为不变闸门**（模板搬后端最容易碰坏的地方）：

- 现有工作流在网页里能打开、能跑；
- `GET /api/v1/tool` 的既有字段一字未变（只**新增**了 `node_template`）；
- `GET /api/v1/flows/{id}` 响应体不变；
- 开放 API 契约测试（`test/open_api/test_openapi_schema_contract.py`）通过。

### 7.4 可观测

- **后端**：沿用既有 `logger.info`（如 `open_endpoints/api/endpoints/flow.py` 里的 `ip` / `flow_id` 记录）与开放 API 审计（`open_api.call` 的审计行）。写端点的审计必须记 `principal.actor_id`（谁在写），不记归属人的名字——归属人是"以谁的身份行事"，actor 才是"谁在行事"。
- **CLI**：`--verbose` 打请求方法 / 路径 / 状态码 / 耗时（`Authorization` 恒掩码）；`--json` 的每条 NDJSON 事件带 `stage`；最后一行恒为 `result` 事件。
- **差异报告**：进 `details` 的差异项必须可定位（节点 id、参数键、连线两端与句柄）——spec AC-20 的要求，也是 agent 自修的唯一输入。

---

## 8. 后续改进 / 已知短板

### 已解除的风险

| 原风险 | 结论 |
|---|---|
| "服务账号的归属人能否改这个工作流这条 FGA 关系是否存在，**需要实测**；若归属人判定不通，v2 写路径落空，push 需退回用户态 JWT" | **已解除**。后端已有 `get_open_api_operator[_async]()` 这条桥（§3 决策 3），它把 principal 转成 `UserPayload` 并对服务账号强制清空管理员角色；v2 端点直接把它喂给既有的 `WorkFlowService.judge_flow_write_permission`（内部就是 `require_business_action(action="edit")`）。**不需要新发明授权关系，也不需要退回用户态 JWT。** 剩余的只是"服务账号的归属人本人对该工作流有没有 edit 权限"这一条正常的业务判定——与网页上换个用户操作是同一条判定 |
| "开放 API 与 v1 的数据一致性：v2 写路径要自己维护 `Flow.data` 镜像，否则会漂移" | **已解除**。本 feature **不自己写镜像**——切当前版本直接调 `WorkFlowService.change_current_version`，镜像由平台内部的 `parent.data = target.data` 完成（§5 坑 18） |
| "节点模板搬后端的规模：前端那份字面量约 890 行，搬的时候要保证 `v` 版本号、`group_params` 结构逐字一致" | 仍是风险，但有对策：搬的时候用**前端那份 TS 字面量作黄金参照**逐字对照，对照通过后把黄金固化成后端测试的期望值（§7.1）。`v` 字段是 `flowCompatible.ts` 的迁移依据，错一个数字会让旧工作流在前端打不开 |

### 已知短板（本期不投入）

| 短板 | 为什么不投 | 重写 / 拆分的触发条件 |
|---|---|---|
| **后端不校验节点级结构**。本 feature 的 12 条校验全在 CLI 侧；一个绕过 CLI 直接打 v2 端点的人可以推一份结构错误的工作流上去，直到运行时才炸 | 后端今天连 YAML 都不读，校验规则放在后端就是**第二份**（决策 1 的理由 3）。而且 spec 把校验定位在"编译期" | 后端新增任何消费 YAML / 声明格式的入口（决策 1 的"触发条件"）；或出现"绕过 CLI 推坏了工作流"的真实事故 |
| **`report` 节点需要平台侧的资源标识**。`report_info.version_key` 指向对象存储上的 docx 模板，导入工作流时前端要调 `POST /api/v1/workflow/report/copy` 换新 key；声明格式凭空生成不出这个 key | 报告模板的复制是平台侧的资源操作，不是声明能表达的 | 把报告节点纳入本 feature 时：先定"声明里怎么表达报告模板"，再定"复制后新 key 怎么回写进 YAML" |
| **Python 覆盖层能执行任意代码** | 用户显式指定 `--overlay` 就是在跑自己的代码；限制它的**结构影响力**（决策 6 的表）比限制它的代码能力实际 | 覆盖层被拿到非可信来源（远程拉来的 overlay）时 |
| **工具列表是全量的**。`flow compile` 每次拉全部工具类型 | 今天工具数量少 | 工具类型超过几百个时（决策 8 的"何时该重新考虑"） |
| **没有"回滚"命令**。回滚 = 用旧版本的声明重新 push 一次 | `flow push` + `git revert` 已经够用 | 出现"只想切回某个旧版本、不想重新编译"的需求时，加 `flow activate --version-id` |
| **YAML DSL 没有 schema 文件**。键名只存在于 CLI 代码里 | 后端不读 YAML，没有第二份需要同步（决策 2 的 AC-44 兑现） | 同"后端消费 YAML"的触发条件 |
| **前端节点库首屏多一次请求** | 换取"只有一份模板" | 请求延迟实测影响首屏时（可加前端侧缓存，但**不是**副本） |

### 与 invest 仓库的关系（本 feature 落地后的迁移动作，不在本 feature 范围内）

1. invest 的 `tools/bisheng/build_v1/v2/v3.py` → 改写为一份 `flow.yaml` + 覆盖层；
2. `tools/bisheng/extract_templates.js` → 删除（模板已由后端提供）；
3. `tools/bisheng/bsflow.py` → 删除（能力进 CLI 包；其中的 `tool_tpl` 是前端逻辑的复刻，前端逻辑一搬家它就必然漂移）；
4. `tools/bisheng/templates.json`（gitignore 的本机产物）→ 不再需要；
5. `tools/bisheng/run_v*.py`（websocket 联调器）→ 用 `bisheng flow run` 替代；
6. `tools/research_mcp/server.py` 的 `invoke()` → 走 v2，鉴权没问题，可保留；其 `startswith("invest")` 的工具名硬编码 → 改为按 operationId 精确匹配；
7. **线上漂移问题被根治**：invest 侧那条工作流（id `1194f8540b9944fea4353b07f0493cbf`）改为 YAML 声明 + `flow diff` 核对，不再靠人手对齐。

---

## 修订历史

| 日期 | 改动 | 触发原因 |
|---|---|---|
| 2026-11-20 | 初版 | feature 设计定稿 |
| 2026-11-20 | 决策 3 增加 `get_open_api_operator[_async]()` 的发现与"原最大风险已解除"的结论 | 核实 v2 身份链时发现后端已有 principal → `UserPayload` 的桥，不需要新造授权关系 |
| 2026-11-20 | 决策 2 补齐 v1 + v2 两个模板端点 | 约束 2（CLI 只有服务账号密钥 ⇒ CLI 能用的后端接口必须在 v2 面） |
| 2026-11-20 | 决策 5 明确"排除画布信息、纳入节点语义字段"的完整比较表 | 避免"只比参数值"漏掉 `tab` / `tool_key` / `name` 的真实差异 |
| 2026-11-20 | **design 自审一轮**：新增 §2 的 Constitution Check（逐条核对 C1–C8）、§4.5 的 AC 落点索引；§4.4 补齐 12 类节点的必填参数表（原表只覆盖 input / llm，不满足 AC-15 的"某类节点"）；§5 补 3 条坑（21 平台资源 id 编译期不可校验 / 22 `output` 节点两种出参形状 / 23 `agent` 的条件必填）；§6 补内部 Python API 与平台服务依赖；决策 2 点明与 AC-01"一个只读接口"的一处读法 | 自审对照 `sdd-review/references/design-checklist.md` 的 24 项检查 |
| 2026-11-21 | G8 两轮 codex 复核后再修 §4.2(c) 与 §4.4：单键引用 `{{#histories#}}` 等在工作流里必崩（实测 `get_variable_by_str` → `IndexError`），不是"特例键"；#8 的扫描范围按引擎实际替换处定（`var_textarea` / `var_textarea_file` / `form` 类型 + 工具节点全部参数 + `output_result` 在 `type=input` 时 + `image_prompt` 与 `code_input` 里的 `ref` 变量），不扫 `code` / `report`；#9 增 `model_id` 非空（llm/agent/rag，`BishengLLM` 拒绝空值）与 `output_msg`/`message`、`code_input`/`code_output` 的形状；#12 的 case `id` 与四个条件字段必须是字符串；条件 `logic` 只认小写 `and`/`or`（引擎把其余一律当 OR）；`note` 节点整体豁免 | codex 逐条探针复现，均对照引擎源码与实测核实后才改 |
| 2026-11-21 | Wave 3 实现对照引擎修正 §4.4：#5 进出边均可；#7 只判条件节点；#8 孤立 `{{#` 不算引用、条件变量也查；#12 case 的 `operator`/`conditions` 可选（原文写成必填的 `logic`/`conditions`）；`agent.sql_agent` 不列必填。§6.5 补齐实现中确定的键：节点 `name`/`description`/`position`、条件 `id`/`ref`/`label`、工具限定名、未知键报错 | 按原表实现会误报合法定义（AC-18）；DSL 用 invest v3 真实工作流做了零差异的编译比对 |
