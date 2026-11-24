# Tasks: 工作流编辑页 copilot

**关联规格**: [spec.md](./spec.md)
**版本**: v3.1.0-beta1

---

## 状态

| 步骤 | 状态 | 备注 |
|------|------|------|
| spec.md | ✅ 已评审 | 2026-11-23 用户确认（codex spec 审查后修订） |
| design.md | ✅ 已评审 | 2026-11-23 用户确认（codex design 审查后修订）；接手时的第一入口 |
| test-cases.md | ✅ 已生成 | QA 参考，18 条 |
| tasks.md | 🔲 草稿 | 拆解完成后改为 ✅ 已拆解 |
| 实现 | 🔲 未开始 | 0 / 24 完成。偏差处理见 design.md 顶部调整原则 + `docs/SDD-Guide.md` §3-§4 |

---

## 开发模式

- **按 Wave 组织**，每个任务标 `依赖:`。
- **后端与 CLI**：Test-First，先写测试、看它红，再写实现。
- **前端**：vitest 覆盖可单测的逻辑（快照比对、撤销、互斥），界面用手动验证。
- **本地跑测试**：
  - 后端：在 `src/backend` 用 `uv run pytest`，需要 gitignore 的 `bisheng/config.yaml` 占位。
  - CLI：在 `src/bisheng-cli` 用 `uv run pytest -m "not network"`。
  - 前端：vitest 有约 40 条既有失败，要先存基线，只比新增失败。
- **换行**：改文件一律按字节读写，提交前检查没有 `\r\n`（仓库 eol=lf）。
- **设计论证**：只写在 design.md，这里只写「做什么、改哪、验收」。

---

## Tasks

### Wave 0 — 探路（决策 1 的推翻条件，先做）

- [ ] **T001**: 中立包打包探路
  **文件**：
  - `src/backend/bisheng_flow_dsl/__init__.py`（临时最小模块）
  - `src/bisheng-cli/pyproject.toml`（`force-include`、pytest `pythonpath`）
  - `scripts/pack_cli_wheel.sh`

  **逻辑**：按 design 决策 1「何时推翻」验证三件事：
  1. 用 Linux 容器的可复现配方打出的安装包里含 `bisheng_flow_dsl`。配方要改为 `git archive` 同时带上 `src/backend/bisheng_flow_dsl`。
  2. 空环境装上安装包后能 import。
  3. CLI 测试跑的是源码：故意改坏中立包，CLI 测试必须变红。

  任一条不成立且无干净解法 → **停下，与用户重新确认**（推翻已确认的决策 1，回到方案 A）。
  **覆盖 AC**：AC-23（前提）
  **依赖**：无

### Wave 1 — 中立包与反编译（CLI 侧）

- [ ] **T002**: 编译 / 校验 / 对比挪入中立包
  **文件**：
  - `src/backend/bisheng_flow_dsl/{compiler,validate,diff,errors}.py`
  - `src/bisheng-cli/bisheng_cli/flow/{compiler,validate,diff}.py`：删除，改为调用方 import 中立包
  - CLI 其余引用处：把 `DslError` 包成 `CliError`，退出码不变
  - `src/bisheng-cli/tests/*`：import 路径更新

  **逻辑**：只挪位置、不改行为。CLI 既有测试（含 F006 的技能包对齐测试）全部照常通过，这就是「行为不变」的证明。
  **测试**：
  - 新增 `src/bisheng-cli/tests/test_dsl_boundary.py`：`bisheng_flow_dsl/` 不 import `bisheng` / `bisheng_cli` / `bisheng_langchain`；`bisheng_cli/` 不 import `bisheng.` / `bisheng_langchain`。
  - 加反向检查：往中立包里放一行 `import bisheng`，边界测试必须红。

  **覆盖 AC**：AC-23
  **依赖**：T001

- [ ] **T003**: 打包与 CI 跟进 + 契约修订
  **文件**：
  - `.github/workflows/cli-quality.yml`（触发路径加 `src/backend/bisheng_flow_dsl/**`）
  - `.github/workflows/backend-quality.yml`（同上，如需要）
  - `src/backend/pyproject.toml` + `uv.lock`（PyYAML 提为直接依赖，版本不变）
  - `dev_toolkit/artifacts/*.whl` + `manifest.json`（用可复现配方重打）
  - `features/v3.1.0-beta1/release-contract.md`（表 1 F005 行改为中立包共享）
  - `src/bisheng-cli/README.md`（改中立包后要 `uv sync --reinstall-package bisheng-cli`）

  **覆盖 AC**：AC-23
  **依赖**：T002

- [ ] **T004**: 反编译测试（先红）
  **文件**：`src/bisheng-cli/tests/test_dsl_decompile.py`
  **测试**：
  - F006 两个样例：编译 → 反编译 → 再编译，`diff` 无差异。
  - 带便签、未识别字段、视角的画布：反编译结果的 `omitted` 逐项列出这些内容。
  - 条件节点（多 case、正则）、工具节点（`tool:` 名称）、批量模式的大模型节点：往返无差异。
  - 老画布缺参数（design 坑 8）：差异只出现在被补上的参数。
  - 保留原节点 id。
  - 反向检查：反编译时丢掉一个参数，往返测试必须红。

  **覆盖 AC**：AC-24、AC-08
  **依赖**：T002

- [ ] **T005**: 反编译与 YAML 输出实现
  **文件**：`src/backend/bisheng_flow_dsl/{decompile,dump}.py`
  **逻辑**：
  - 画布 → 声明：按模板识别参数；只写与模板默认值不同的参数；工具节点写 `tool: <名称>`；条件写 `cases`；连线写 `from` / `to` / `handle`；写 `position`。
  - 输出 `(声明, omitted)`。
  - `dump`：中文不转义，多行字符串用 `|`，键序固定。

  **测试**：T004 全部通过
  **覆盖 AC**：AC-24
  **依赖**：T004

- [ ] **T006**: `bisheng flow export`（测试 + 实现）
  **文件**：
  - `src/bisheng-cli/bisheng_cli/cli.py`、`commands/flow.py`
  - `src/bisheng-cli/tests/test_command_flow_export.py`
  - `dev_toolkit/skills/workflow-as-code/SKILL.md`（补 export 用法，过 F006 对齐测试）
  - 重打随附安装包

  **逻辑**：
  - 读线上当前版本 → 反编译 → 写文件。
  - 被略过的内容：stderr 逐项列出，`--json` 放 `details.omitted`。
  - 每条退出路径恰好输出一个 `result` 事件。

  **覆盖 AC**：AC-24
  **依赖**：T005

### Wave 2 — 后端 copilot

- [ ] **T007**: 错误码与文案
  **文件**：
  - `src/backend/bisheng/common/errcode/flow.py`（10560–10563）
  - `src/frontend/packages/locales/src/api_errors/*.json`（三语）
  - release-contract「已分配模块编码」一节

  **逻辑**：`Code: int = 105xx` 写法，让 `pnpm check-i18n` 能识别。
  **覆盖 AC**：AC-03、AC-27
  **依赖**：无

- [ ] **T008**: 资源清单与名称解析测试（先红）
  **文件**：`src/backend/test/workflow/copilot/test_resources.py`
  **测试**：
  - 唯一匹配填 id，且 id 为数字（design 坑 7）。
  - 无匹配 / 多候选：模型填 0、知识库填空列表，并写入 `needs_selection`（含候选，最多 10 个）。
  - 用户手写无权 id 被清空（AC-16）。
  - 下线模型不算可用。
  - 按模板参数类型识别资源参数：往模板加一个 `bisheng_model` 参数，它自动被识别。
  - 工具名对不上：节点被移除并进入 `needs_selection`（design 决策 4）。
  - 知识库游标翻页。

  **覆盖 AC**：AC-14、AC-15、AC-16、AC-26
  **依赖**：T002

- [ ] **T009**: 资源清单与名称解析实现
  **文件**：`src/backend/bisheng/workflow/domain/services/copilot/resources.py`
  **逻辑**：取数接口按 design 决策 4（已核实签名）。
  **测试**：T008 全部通过
  **依赖**：T008

- [ ] **T010**: 合并与布局测试（先红）
  **文件**：`src/backend/test/workflow/copilot/test_merge.py`
  **测试**：
  - 新增、删除、改参、改连线各自正确。
  - 便签与未识别字段保留，未改节点的位置不变。
  - 同 key 多值按顺序覆盖；数量对不上时整节点替换并做标记。
  - 新节点放在原画布包围盒右侧、互不重叠。
  - 差异口径：`diff(local=方案, online=发送时画布)`，`nodes_only_local` 为新增。
  - 应用的结果与预览的差异一致：对合并结果再算一次差异，应等于预览的差异。

  **覆盖 AC**：AC-08、AC-09、AC-17、AC-18
  **依赖**：T002

- [ ] **T011**: 合并与布局实现
  **文件**：`src/backend/bisheng/workflow/domain/services/copilot/merge.py`
  **测试**：T010 全部通过
  **依赖**：T010

- [ ] **T012**: 提示词拼装（测试 + 实现）
  **文件**：
  - `copilot/prompt.py`
  - `src/backend/test/workflow/copilot/test_prompt.py`

  **逻辑**：前言 + 整份 SKILL.md + 资源名称清单 + 当前声明 + 对话历史。超长（声明超过 60 000 字符或超过 20 轮）抛 10562。
  **测试**：
  - 技能包内容确实来自随包文件。
  - 超长报错。
  - 历史里 assistant 的内容不带 yaml。

  **覆盖 AC**：AC-07、AC-22、spec §3 大画布
  **依赖**：T005、T007

- [ ] **T013**: copilot 服务测试（先红，假模型）
  **文件**：`src/backend/test/workflow/copilot/test_service.py`
  **测试**：用脚本化的假模型覆盖：
  - 一次通过；重试后通过；3 次都失败时返回 `issues`。
  - `!include` 被拒；代码节点被挡；纯问答不出方案。
  - 默认模型未配置 / 停用；模型调用异常。

  另外检查日志里不出现画布与对话原文。
  **覆盖 AC**：AC-03、AC-05、AC-06、AC-10、AC-12、AC-13、AC-25、AC-27
  **依赖**：T009、T011、T012

- [ ] **T014**: copilot 服务实现
  **文件**：`src/backend/bisheng/workflow/domain/services/copilot/service.py`
  **逻辑**：design 决策 2、6、7。模型调用在生成器内直接 await，不放线程。
  **测试**：T013 全部通过
  **依赖**：T013

- [ ] **T015**: 端点测试（先红）
  **文件**：`src/backend/test/workflow/copilot/test_endpoint.py`
  **测试**：
  - 无编辑权限被拒，且未调用模型。
  - 流开始前的错误返回普通 JSON。
  - 事件顺序与结构。
  - 模拟客户端断开时，假模型的调用收到取消（守住 design 决策 7 的前提）。

  **覆盖 AC**：AC-04、AC-11、AC-17
  **依赖**：T014

- [ ] **T016**: 端点实现
  **文件**：`src/backend/bisheng/api/v1/workflow.py`（`POST /workflow/copilot`）
  **测试**：T015 全部通过；跑 arch-guard
  **依赖**：T015

### Wave 3 — 前端

- [ ] **T017**: 流式请求支持请求体 + copilot 请求函数
  **文件**：
  - `src/frontend/platform/src/controllers/requestStream.ts`（可选的 body 参数，默认 `{}`，现有调用不变）
  - `controllers/API/workflow.ts`

  **测试**：vitest 覆盖带 body 与默认 body 两种情况
  **依赖**：T016

- [ ] **T018**: 编辑页接线：应用回调与面板互斥
  **文件**：
  - `pages/BuildPage/flow/Panne.tsx`（应用回调：先 `takeSnapshot` 再落画布）
  - `Header.tsx`、`FlowChat/ChatTest.tsx`（加 `onOpenChange`）

  **测试**：vitest 验证：
  - 应用后一次撤销即回到原画布；
  - 打开测试面板会收起 copilot，反之亦然。

  **覆盖 AC**：AC-02、AC-18
  **依赖**：无

- [ ] **T019**: copilot 抽屉
  **文件**：`pages/BuildPage/flow/Copilot/{CopilotDrawer,DiffPreview,canvasDigest}.tsx|ts`
  **逻辑**：
  - 输入区用 `ConversationComposer`；对话状态提升到 Panne。
  - 差异预览显示本地化的参数名，并标出「需要你选」。
  - 默认模型不可用时禁用发送。
  - AC-19：用 `canvasDigest` 规范化比对，去掉 `selected` / `dragging` / `measured`。
  - 停止 = abort。

  **测试**：vitest 验证：
  - `canvasDigest` 不受选中状态影响，但拖动节点会改变它；
  - 未应用时画布不变。

  **覆盖 AC**：AC-01、AC-03、AC-11、AC-17、AC-19、AC-20、AC-21、AC-27
  **依赖**：T017、T018

- [ ] **T020**: 前端文案三语
  **文件**：`platform/public/locales/{zh-Hans,en-US,ja}/flow.json`
  **测试**：`pnpm check-i18n`
  **依赖**：T019

### Wave 4 — 收尾

- [ ] **T021**: 全量回归
  **逻辑**：
  - 后端 copilot 与相关测试、CLI 全量测试、CI 用的 ruff 精确清单。
  - 前端 vitest：比对基线，只看新增失败。
  - `pnpm lint`、`pnpm typecheck`、`pnpm check-i18n`。

  **依赖**：T006、T016、T020

- [ ] **T022**: 端到端（隔离环境）
  **逻辑**：按 design §7 与 test-cases.md 走 TC-CPL-001～018。模型服务需用户授权后再配。
  **依赖**：T021

- [ ] **T023**: G8 双轮审查
  **逻辑**：codex 两轮（第一轮无问题也要做第二轮），结论自己复跑验证。
  **依赖**：T021

- [ ] **T024**: 文档收尾与 PR
  **逻辑**：
  - release-contract 变更历史；design 修订历史。
  - 偏差记录回写。
  - 在 fork 内开 PR，目标分支 `codex/3.1.0-beta1-sdd-opt`。

  **依赖**：T022、T023

---

## 实际偏差记录

（实现中填写；只留一行指针，论证写在 design.md。）
