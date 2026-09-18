# Review Flow Notes

这份笔记只记录本轮实际运行的现有主链，不提出新架构。

## Runtime evidence

使用 `tests/test_phases_0_5.py::FakeChatClient`、临时 SQLite、随机本地 HTTP 端口，实际向 `POST /v1/reviews` 提交含 `eval(user_input)` 的 unified diff。

- HTTP：201
- API state / persisted task state：`SUCCESS / SUCCESS`
- task trace：`PLANNING → EXECUTING → REVIEWING → SUCCESS`
- Agent roles：Lead、Security、Correctness/Reliability、Critic
- LLM calls：6
- Worker statuses：两个 assignment 均 `completed`
- Critic：接受 finding 0
- Lead final：选择 finding 0
- FindingGate：accepted=1、rejected=0
- Report：1 个 `SEC-EVAL`，overall risk=`high`
- Checkpoints：`planning`、`executing`、`reviewing`、`agentic-lead-session`、`agentic-summary`

## Existing call chain

| Step | File / function | Input | Output | Next caller |
|---|---|---|---|---|
| 1 | `evoagent/api.py:306-367 ApiHandler.do_POST()` | JSON repository、PR、diff、mode、可选 roles/skills | 参数校验后调用同步或异步 service | `ReviewService.create_review()` |
| 2 | `evoagent/service.py:275-311 ReviewService.create_review()` | 经过 API 解析的 review 参数 | 建 task，运行 review，返回 task id/state/report | `_create_task()`，再 `_run_review()` |
| 3 | `evoagent/service.py:241-259 _create_task()` | repository/diff/tenant/selection | SQLite task、diff payload、release assignment | `ReviewHarness.run()` |
| 4 | `evoagent/harness.py:54-112 ReviewHarness.run()` | task id、diff、repository | 执行三节点 durable workflow，成功后持久化 report | `AgentRuntime.execute()` |
| 5 | `evoagent/runtime.py:140-218 AgentRuntime.execute()` | initial state + planning/executing/reviewing nodes | 合并各 node dict 输出并保存 checkpoint | 各 RuntimeNode handler |
| 6 | `evoagent/harness.py:120-140 _planning/_executing()` | raw diff；随后 ParsedDiff | planning 产生 parsed；executing 调 reviewer 并产生 Finding dicts | `AgenticReviewer.review_with_context()` |
| 7 | `evoagent/agentic_core.py:322-424 review_with_context/_review_with_context()` | task/diff/ParsedDiff/repository/tenant | resolved mode、context、tool suite、Agent findings、gate summary | `_agentic()`；随后 `FindingGate.apply()` |
| 8 | `evoagent/agentic_core.py:512-556 _agentic()` scanner + Lead delegation | diff、scanner findings、available Skills/workers | delegations、risk level、lead session checkpoint | `_run_pending_assignments()` |
| 9 | `evoagent/agentic_core.py:900-1002 _run_pending_assignments()` | Lead assignments、managed diff/context/tools | 并行 Security 与 Correctness/Reliability worker results | `_session_candidates()` |
| 10 | `evoagent/agentic_core.py:227-261 _parse_findings()` | Worker final JSON | 只保留可构造、指向 added line 的 Finding | `_merge()` |
| 11 | `evoagent/agentic_core.py:1124-1128,1244-1256` | scanner + worker findings | 按 path/line/canonical identity 去重、按 severity 排序 | Critic |
| 12 | `evoagent/agentic_core.py:636-650,851-898` | blinded candidates + Lead critic objective | index-based accept/objection/confidence decisions | Lead final |
| 13 | `evoagent/agentic_core.py:661-695,1161-1182` | candidates + critic decisions + worker results | Lead 选择最终 indices，session 变为 completed | `_review_with_context()` |
| 14 | `evoagent/gates.py:25-91 FindingGate.apply()` | Lead 已选择 findings + ParsedDiff | format/evidence/confidence/release gates；accepted/rejected | `ReviewHarness._reviewing()` |
| 15 | `evoagent/harness.py:142-169 _reviewing()` | gated Finding dicts + collaboration summary | `ReviewReport` | `store.succeed()` / API JSON |
| 16 | `evoagent/report.py:4-51 to_markdown()` | persisted report dict | Markdown | report endpoint / GitHub comment path |

实际顺序中的关键点：Critic 不直接删除 candidates；其 decisions 交给 Lead final。确定发布列表之后，`FindingGate` 才做最终 deterministic gate。

