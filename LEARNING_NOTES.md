# EvoAgent Learning Notes

只记录现有代码和本轮 runtime 证据。

## 1. Review 请求如何进入系统

`ApiHandler.do_POST()` 在 `api.py:306-367` 解析 `/v1/reviews`，验证 JSON/roles/skills 后调用 `ReviewService.create_review()` 或 `enqueue_review()`。GitHub 路径在 `api.py:368-395` 验签后调用 webhook service。`[RUNTIME]` fake-model HTTP 请求返回 201 和完整 report。

## 2. Task 如何持久化

`ReviewService._create_task()`（`service.py:241-259`）生成 UUID，把 input metadata 写入 store，并把 diff 单独写入 `task_payloads`。SQLite schema 在 `store.py:26-139`；状态、trace、report、checkpoint 分表保存。PostgreSQL 由 `create_store()` 在配置 database URL 时选择。

## 3. Harness / AgentRuntime 的职责区别

`ReviewHarness` 定义 review 业务节点、TaskState 转换、Finding/Report 序列化以及最终 success/failure（`harness.py:37-169`）。`AgentRuntime` 是通用顺序节点执行器，负责预算、重试、取消和 checkpoint 恢复（`runtime.py:123-218`），不知道 PR、Finding 或 Report 的业务含义。

## 4. Lead 到底负责什么

Lead prompt 明确负责 decomposition、delegation、最多一次高风险 revision 和 final synthesis（`agentic_core.py:25-48`）。运行中 Lead 先产生 worker assignments；高风险时可评估 worker results 并请求 revision；最后依据 candidates 和 Critic decisions 返回 accepted indices（`agentic_core.py:523-695`）。

## 5. Worker 如何调用模型和工具

`_run_pending_assignments()`（`agentic_core.py:900-1002`）根据 worker role 构造 prompt/context，注入 Lead assignment 和选中的 Agent Skills。多个 pending assignments 通过 `ThreadPoolExecutor` 并行。`BoundedRole.run()`（`agentic_core.py:130-224`）在 step/token/time budget 内调用 model；模型可返回一个 tool action，再由 role 调 ToolRegistry 后继续，也可直接 final。

## 6. Finding 如何生成和 parse

deterministic scanners 和 Worker model 都能产生 Finding。Worker final JSON 经 `_parse_findings()`（`agentic_core.py:227-261`）转换；无效 severity、path、line 或非 added-line location 会被丢弃。`Finding` 的实际字段定义在 `models.py`，包含 rule/severity/title/explanation/location/evidence/fix/test/confidence 等。

## 7. scanner / worker finding 如何 merge

`_session_candidates()` 先恢复 scanner findings，再追加每个 worker result（`agentic_core.py:1124-1128`）。`_merge()` 使用 `(path,line,canonical_identity(rule_id,cwe))` 去重；冲突时保留 confidence 更高者，再按 severity/path/line 排序（`agentic_core.py:1244-1256`）。

## 8. Critic 的真实职责

Critic 收到去除来源身份的 candidates，检查 counterexample、错误位置、前置条件和 severity，不允许创造新 finding（`agentic_core.py:69-74,851-898`）。`_apply_critic()` 将 index decision 和置信度调整写回，但不直接过滤 candidates（`agentic_core.py:1130-1159`）。

## 9. Lead final 的职责

Lead final 接收 candidates、Critic decisions 和 worker results，返回 `accepted_finding_indices` 及可选 confidence adjustments（`agentic_core.py:661-688`）。`_apply_lead_final()` 才从 candidates 中取出发布候选（`agentic_core.py:1161-1182`）。

## 10. FindingGate 什么时候执行

Lead final 之后，`_review_with_context()` 调 `FindingGate.apply()`（`agentic_core.py:389-424`）。Gate 按 required fields/added-line location、evidence、minimum confidence 和 release 条件接受或拒绝（`gates.py:25-91`）。`[RUNTIME]` fake review 的 1 条 finding 通过所有 gate。

## 11. Report 如何生成

Harness 的 reviewing node 从 gated findings 计算 risk/summary，附加 collaboration、run mode、components、execution 后构造 `ReviewReport`（`harness.py:142-169`）。成功 report 写入 task；`report.py::to_markdown()` 用于 report endpoint 和 GitHub comment。

## 12. Prompt 如何加载

Lead/Security/Reliability/Critic 的基础 prompts 是 `agentic_core.py:25-74` 常量。`ReviewService._build_agentic_reviewer()` 在构建 reviewer 时读取数据库 active `llm-review` prompt，并作为 overlay 传入（`service.py:144-183`）。模型 transport 使用 settings 解析的 provider/model。

## 13. Skill 如何加载与选择

Agent Skill 是带 YAML frontmatter 的 `SKILL.md` prompt-time capability，不是可执行 Python reviewer（`skills.py:1-6,35-112`）。`SkillRegistry.reload()` 扫描 `skills/*/SKILL.md`。Service 用 tenant active DB artifact 覆盖同名磁盘 Skill（`service.py:208-215`）。Lead 从 catalog 选择 Skill 放进 assignment；Worker 才得到完整 instructions 和允许 tools。

## 14. Feedback 如何记录

`POST /v1/tasks/{id}/feedback` 调 `ReviewService.record_feedback()`（`service.py:482-499`）。仅完成任务可记录；category 限于 `false_positive/missed_issue/bad_fix/accepted`。数据写 `failure_cases`，同时写 repository/tenant scoped memory。Harness execution exception 也以 `execution_error` 写 failure case（`harness.py:100-109`）。

## 15. Prompt evolution 如何运行

`EvolutionEngine.auto_propose()` 从 unresolved failure cases 取信号；有 generator 时生成 candidate prompt，否则走有限的规则式追加逻辑（`evolution.py:484+`）。`_propose()` 做 prompt safety/completeness、validation replay、holdout non-regression 和 improvement gate，保存 candidate version/run；通过时可 activated，structured auto path 使用 shadow policy（`evolution.py:343-478`）。

## 16. Skill evolution 如何运行

`SkillEvolutionEngine.auto_propose()` 从 feedback 中提取合法 rule id；missed issue 可向 `SKILL.md` 加 learned block，false positive 可移除已有 learned block（`skill_evolution.py:377-435`）。`propose()` 验证 artifact，用 model-backed Agent Skill reviewer 在 validation/holdout replay，通过 improvement/non-regression gates 后保存并激活（`skill_evolution.py:271-375`）。

## 17. Version activation / rollback 如何生效

Prompt version 激活由 `store.activate_skill_version()` 切换 `skill_versions.active`（`store.py:672-683`）；Skill artifact 由 `activate_skill_artifact()` 切换 tenant/name 下 active row，并只允许曾成功 activated 的版本（`store.py:756-778`）。两个 engine 的 `rollback()` 都只是调用对应 active-pointer 切换。新 AgenticReviewer 构建或 `reload_skills()` 后读取 active prompt/artifact；已存在 reviewer object 不会被数据库更新原地改写。

