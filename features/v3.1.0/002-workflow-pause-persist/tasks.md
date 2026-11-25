# Tasks: 工作流暂停态持久化

**关联规格**: [spec.md](./spec.md)
**版本**: v3.1.0

---

## 状态

| 步骤 | 状态 | 备注 |
|------|------|------|
| spec.md | ✅ 已评审 | 2026-11-25 用户确认 |
| design.md | ✅ 已评审 | 2026-11-25 用户确认 |
| test-cases.md | ✅ 已生成 | 16 条 |
| tasks.md | ✅ 已拆解 | — |
| 实现 | 🔄 进行中 | 8 / 9 完成，剩 T009 评审 |

---

## 开发模式

后端 Test-First：先写失败的测试，再写实现。中间件相关的集成与端到端放到隔离环境验证。每个任务内联文件、逻辑与 AC 覆盖。

---

## Tasks

### Wave 1（无依赖）

- [x] **T001**: 错误码与配置
  **文件**: `src/backend/bisheng/common/errcode/flow.py`、`src/backend/bisheng/core/config/settings.py`
  **逻辑**: 先核对 10544 至 10546 未被占用；新增 10544 会话已过期、10545 暂停态无法还原；`WorkflowConf` 新增 `pause_retention_seconds`（默认 604800，非法值回落默认，参照 `code_node_enabled` 的校验写法）。
  **覆盖 AC**: AC-07、AC-08、AC-11、AC-12
  **依赖**: 无

- [x] **T002**: 引擎导出与导入运行状态（测试先行）
  **文件**: `src/backend/bisheng/workflow/graph/graph_engine.py`、`src/backend/test/workflow/test_graph_engine_state.py`
  **逻辑**: 增加 `export_run_state()` 与 `import_run_state()`：检查点存储、变量池、历史消息、各节点 `current_step`；序列化用 LangGraph 自带序列化器；对 `MemorySaver` 内部结构加断言测试。往返后 `get_state().next` 一致。
  **覆盖 AC**: AC-01、AC-03、AC-04
  **依赖**: 无

### Wave 2

- [x] **T003**: 暂停态服务（测试先行）
  **文件**: `src/backend/bisheng/workflow/domain/services/pause_state.py`、`src/backend/test/workflow/test_pause_state.py`
  **逻辑**: 保存（带格式版本、租户、工作流、暂停节点、画布副本、保留期）、读取并校验、删除、续跑令牌的抢占与释放；序列化失败返回可读原因；日志只记长度与键名。
  **覆盖 AC**: AC-01、AC-07、AC-08、AC-09、AC-11、AC-12、AC-13、AC-14
  **依赖**: T001、T002

- [x] **T004**: 暂停时保存、续跑时重建
  **文件**: `src/backend/bisheng/worker/workflow/tasks.py`
  **逻辑**: `_judge_workflow_status` 在 INPUT 时调用保存，失败则置失败并给出原因；`_continue_workflow` 在内存字典缺失时读快照并用画布副本重建 `Workflow`（参数与首次一致，含 `redis_callback.workflow` 赋值），再导入状态；过期返回 10544，无法还原返回 10545，抢令牌失败返回 10546。
  **覆盖 AC**: AC-02、AC-05、AC-06、AC-08、AC-11、AC-12、AC-13
  **依赖**: T003

- [x] **T005**: 结束与停止时清理
  **文件**: `src/backend/bisheng/worker/workflow/tasks.py`
  **逻辑**: 成功、失败、停止路径删除快照；`stop_workflow` 在内存缺失时不重建，直接置失败并删除快照；过期清理时同时移出内存字典。
  **覆盖 AC**: AC-09、AC-10
  **依赖**: T003

### Wave 3

- [x] **T006**: 集成测试（含 Redis）
  **文件**: `src/backend/test/workflow/test_pause_resume_integration.py`
  **逻辑**: 暂停后清空进程字典再续跑；并发两次只有一次执行；停止；过期；损坏；Redis 不可用。
  **覆盖 AC**: AC-02、AC-08 至 AC-12、AC-15
  **依赖**: T004、T005

- [x] **T007**: 节点类型覆盖验证
  **文件**: `src/backend/test/workflow/test_pause_state_nodes.py`
  **逻辑**: 对含 LLM（假模型）、检索、工具、条件、循环的样例各跑一次暂停、清空、续跑，确认变量池都可序列化；发现不可序列化的类型记入 design §5。
  **覆盖 AC**: AC-03、AC-04
  **依赖**: T004

### Wave 4

- [x] **T008**: 隔离环境端到端
  **文件**: workflow-study `b1b.py` 场景
  **逻辑**: 新库、本分支后端；跑 TC-PSE-001、002、004、008、010、012 并记录结果；修复前后对比。
  **覆盖 AC**: AC-02、AC-04、AC-08、AC-10、AC-12
  **依赖**: T006、T007

- [ ] **T009**: 文档与评审
  **文件**: `features/v3.1.0/002-workflow-pause-persist/*`、`release-contract.md`
  **逻辑**: 回写实际偏差；design §5 补充实测坑；`/sdd-code-review`；codex 两轮审查（用户全局规则）。
  **依赖**: T008

---

## 实际偏差记录

（实现中填写；只留一行指针，论证写在 design.md。）

- T002 检查点导出改走公共接口并只存最新一份，不读 `MemorySaver` 内部结构 → design.md 修订历史 2026-11-25 第 2 条。
- T003 键名沿用现有 `workflow:{unique_id}:…` 形式、租户号写在快照里校验；新增小标记键 `pause_meta`（保留期之外多留一天）使迟到的请求得到“已过期”而不是“找不到” → design.md 修订历史 2026-11-25 第 3 条。
- T004 不新增错误码 10546；重复输入沿用 10543，续跑令牌败者静默退出 → design.md 决策 5 与修订历史。
- T006 先用现有 `dialogue` 进程内测试台（真引擎、真节点、内存 Redis）完成：暂停保存、丢失对象后续跑、结束清理、旧会话、过期、损坏、循环两次、停止；含 Redis 的真实集成留给 T008。
- T007 先在单元层验证变量池里常见的值：LangChain 文档、pydantic 引用条目、嵌套字典都能往返；LLM 节点已由进程内测试台覆盖；工具、检索节点的真实输出留给 T008 抽查。
- T008 在隔离环境（真实 Redis、Celery Worker 容器，新库 `bisheng_b1`，独立 OpenFGA）实测：暂停后重启 Worker 续跑通过；停止、过期（10544）、清理通过；并发输入只执行一次。**与 spec 的差异**：开放 API 上第二个并发请求拿到只含 `close` 的空流，不是错误（现有行为，非本修复引入）；WebSocket 上重放输入仍被 10543 拒绝（现有测试覆盖）。AC-12 的“明确的拒绝”在开放 API 上未满足，待用户决定是放宽 AC 还是另起修复。
- T008 发现并修复：保留期原先只在重建路径检查，仍在内存里的热缓存会越过保留期；已在热路径也检查（`ensure_not_expired`）→ design.md 修订历史。
- T008 环境说明：3.1.0 基线没有开放 API 的 `workflow:write` 范围与节点模板接口，工作流用平台接口创建；Windows 上仓库不可直接运行（`os.O_NONBLOCK`），测试在 Linux 容器内执行。
- T009 第一轮评审（作者通读加 zen codereview 工作流）发现并修复两项：热缓存过时、快照无大小上限 → design.md 修订历史与 §5 坑 7。zen 的外部专家模型（Gemini）因免费额度耗尽（429）未能给出验证意见。
- T009 第二轮评审（codex，3.6M 输入令牌的深度审查）：5 高 8 中 7 低。已修复并有测试：令牌覆盖所有路径且先抢后读、令牌带持有者与随运行时限的存活期、Redis 故障不破坏快照、暂停时先放内存、停止后不被复活、标记先于快照写入、大小按字节计、导入宽容节点变化与记录 langgraph 版本、热路径不读设置。**未修复，已记录**：M4 进程内对象在用户不回来时不会主动回收（需要定时清理，另起）；M2 抢不到令牌的任务静默退出，客户端在极端情况下可能看到“任务忙”；L3 基础设施错误文本会返回给客户端（既有行为）；L4 停止分支删除状态时不校验租户（上游已校验会话主体）；L5 `workflow_client.py` 记录用户输入原文（既有行为，与 AC-14 冲突，需要另行处理）。codex 在其环境里未能运行测试套件，本地测试结果见 PR。
- 遗留项处理：内存里停放超过保留期的对象在每次暂停时被清扫；`workflow_client.py` 的 INFO 日志不再含用户输入原文（回归测试 `test_user_input_not_logged.py`，已用旧代码验证会失败）；工具节点变量序列化经真实引用函数验证。**用户已决策（2026-11-25）**：AC-12 放宽为“只执行一次，对外拒绝沿用现有机制”；AC-08 保持一天的提示窗口并写进 AC。spec.md 与 test-cases.md 已同步。
