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
| 实现 | 🔲 未开始 | 0 / 9 完成 |

---

## 开发模式

后端 Test-First：先写失败的测试，再写实现。中间件相关的集成与端到端放到隔离环境验证。每个任务内联文件、逻辑与 AC 覆盖。

---

## Tasks

### Wave 1（无依赖）

- [ ] **T001**: 错误码与配置
  **文件**: `src/backend/bisheng/common/errcode/flow.py`、`src/backend/bisheng/core/config/settings.py`
  **逻辑**: 先核对 10544 至 10546 未被占用；新增 10544 会话已过期、10545 暂停态无法还原、10546 续跑被并发拒绝；`WorkflowConf` 新增 `pause_retention_seconds`（默认 604800，非法值回落默认，参照 `code_node_enabled` 的校验写法）。
  **覆盖 AC**: AC-07、AC-08、AC-11、AC-12
  **依赖**: 无

- [ ] **T002**: 引擎导出与导入运行状态（测试先行）
  **文件**: `src/backend/bisheng/workflow/graph/graph_engine.py`、`src/backend/test/workflow/test_graph_engine_state.py`
  **逻辑**: 增加 `export_run_state()` 与 `import_run_state()`：检查点存储、变量池、历史消息、各节点 `current_step`；序列化用 LangGraph 自带序列化器；对 `MemorySaver` 内部结构加断言测试。往返后 `get_state().next` 一致。
  **覆盖 AC**: AC-01、AC-03、AC-04
  **依赖**: 无

### Wave 2

- [ ] **T003**: 暂停态服务（测试先行）
  **文件**: `src/backend/bisheng/workflow/domain/services/pause_state.py`、`src/backend/test/workflow/test_pause_state.py`
  **逻辑**: 保存（带格式版本、租户、工作流、暂停节点、画布副本、保留期）、读取并校验、删除、续跑令牌的抢占与释放；序列化失败返回可读原因；日志只记长度与键名。
  **覆盖 AC**: AC-01、AC-07、AC-08、AC-09、AC-11、AC-12、AC-13、AC-14
  **依赖**: T001、T002

- [ ] **T004**: 暂停时保存、续跑时重建
  **文件**: `src/backend/bisheng/worker/workflow/tasks.py`
  **逻辑**: `_judge_workflow_status` 在 INPUT 时调用保存，失败则置失败并给出原因；`_continue_workflow` 在内存字典缺失时读快照并用画布副本重建 `Workflow`（参数与首次一致，含 `redis_callback.workflow` 赋值），再导入状态；过期返回 10544，无法还原返回 10545，抢令牌失败返回 10546。
  **覆盖 AC**: AC-02、AC-05、AC-06、AC-08、AC-11、AC-12、AC-13
  **依赖**: T003

- [ ] **T005**: 结束与停止时清理
  **文件**: `src/backend/bisheng/worker/workflow/tasks.py`
  **逻辑**: 成功、失败、停止路径删除快照；`stop_workflow` 在内存缺失时不重建，直接置失败并删除快照；过期清理时同时移出内存字典。
  **覆盖 AC**: AC-09、AC-10
  **依赖**: T003

### Wave 3

- [ ] **T006**: 集成测试（含 Redis）
  **文件**: `src/backend/test/workflow/test_pause_resume_integration.py`
  **逻辑**: 暂停后清空进程字典再续跑；并发两次只有一次执行；停止；过期；损坏；Redis 不可用。
  **覆盖 AC**: AC-02、AC-08 至 AC-12、AC-15
  **依赖**: T004、T005

- [ ] **T007**: 节点类型覆盖验证
  **文件**: `src/backend/test/workflow/test_pause_state_nodes.py`
  **逻辑**: 对含 LLM（假模型）、检索、工具、条件、循环的样例各跑一次暂停、清空、续跑，确认变量池都可序列化；发现不可序列化的类型记入 design §5。
  **覆盖 AC**: AC-03、AC-04
  **依赖**: T004

### Wave 4

- [ ] **T008**: 隔离环境端到端
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
