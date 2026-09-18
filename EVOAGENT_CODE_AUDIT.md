# EvoAgent Code Audit

审计日期：2026-09-16  
审计对象：`/Users/bytedance/Downloads/EvoAgent(3)`  
审计方式：静态代码追踪 + 不联网、无外部密钥、无依赖安装的安全运行检查。除本报告外未修改源码、配置、数据库或测试数据。

证据标签：

- `[CODE]`：可由当前工作区代码直接确认。
- `[RUNTIME]`：已在当前机器实际执行并观察到结果。
- `[INFER]`：由多处代码组合推断，尚无端到端运行证据。
- `[UNKNOWN]`：当前仓库和本次安全运行无法确认。

## 1. Executive Summary

### 结论

- `[CODE]` EvoAgent 不是只有 README 和空壳类。它真实实现了一个 Python 3.11、标准库 HTTP Server 驱动的 PR diff 审查服务：接收 API 或 GitHub webhook，持久化任务，经带 checkpoint 的状态机调用 Lead、两个 Specialist、Critic，再由 Lead 汇总成结构化 `ReviewReport`；也包含 GitHub 评论回写、修复分支、认证、队列、SQLite/PostgreSQL、Redis、静态管理页和指标接口。核心入口见 `evoagent/__main__.py:1-5`、`evoagent/api.py:572-587`、`evoagent/service.py:275-384`、`evoagent/harness.py:54-169`。
- `[CODE]` “Multi-Agent” 有真实的多次模型调用和并发 Specialist，不是单 prompt 改名：正常路径是 Lead 分派 1 次、两个 Worker 各 1 次、Critic 1 次、Lead 汇总 1 次；Worker 使用 `ThreadPoolExecutor` 并行。测试中的 fake client 也断言普通路径 5 次调用。见 `evoagent/agentic_core.py:483-695,900-1002`、`tests/test_service.py:23-47`。
- `[CODE]` 但它不是开放式 Agent 编排框架。角色、角色 prompt、工具权限、流程和最多一轮返工均硬编码在 `agentic_core.py`。Lead 只能改变分工目标、文件和 Skill；即使 Lead 漏掉某个 Worker，代码也会补默认 assignment，因此 Lead 并不真正决定是否启用 Specialist。见 `evoagent/agentic_core.py:25-93,483-635,1005-1054`。
- `[CODE]` Self-Evolution 有两条真实实现：全局 `llm-review` prompt version 演化，以及租户级 `SKILL.md` artifact 演化。两者都会生成候选、在 validation/holdout 上回放、保存版本和评测运行，并有 active pointer 与 rollback。见 `evoagent/evolution.py:213-585`、`evoagent/evolution_v2.py:10-75`、`evoagent/skill_evolution.py:161-435`。
- `[CODE]` 这两条演化链并不等价。Prompt 的 LLM 自动生成只产生一个候选并停在 `shadow_ready`；当前请求路由不会按 canary/shadow lane 加载候选 prompt，`observe_shadow()` 也无调用者，所以“失败 → 自动候选 → 影子验证 → 自动上线”没有端到端闭环。Skill 自动演化是基于反馈类别拼接/删除固定 Markdown block，门禁通过后可以直接激活。见 `evoagent/evolution.py:484-585`、`evoagent/rollout.py:10-65`、`evoagent/service.py:144-183,241-259,301-310`。
- `[CODE]` Failure 没有统一 typed domain model。人工反馈和顶层执行异常被写入 `failure_cases` 表，核心形态是 `category + payload_json + resolved`；Worker 局部失败、FindingGate 拒绝、Critic 拒绝、评测 FP/FN 不会统一进入该表。见 `evoagent/store.py:42-49,483-547`、`evoagent/service.py:482-499`、`evoagent/harness.py:100-111`。
- `[CODE]` 现有 trace 足以重建“阶段骨架”和多数结构化决策，但不足以重建精确 decision trajectory：没有保存每次完整 system/user prompt、压缩后的实际上下文、原始模型响应、完整 tool result，以及任务所绑定的 prompt/skill/config version 快照。见 `evoagent/models.py:94-104`、`evoagent/telemetry.py:1-134`、`evoagent/agentic_core.py:111-224,1229-1242`。
- `[RUNTIME]` 当前 `evaluation_data/pr_diff_100.jsonl` 可被底层 loader 读取，共 100 case；直接用确定性 baseline/context reviewer 回放，得到 F1 `0.7143 → 0.8250`、High-risk Recall `0.8421 → 0.9474`、Clean PR Accuracy `0.9167`。这些数字可由当前数据和规则复现，但官方 controlled benchmark 入口当前因缺少 PyYAML 先失败；即使安装后，脚本还要求 `source.kind=offline-fixture`，而数据是 `synthetic-controlled`。因此数字是“部分可复现”，不是可复现的真实 PR/LLM Agent 效果。
- `[RUNTIME]` 当前宿主只有 `/usr/bin/python3` 3.9.6，`python` 命令不存在；项目声明 Python 3.11。未安装 PyYAML 等 requirements。fallback discovery 实际通过 21 项，出现 9 个 unittest error entry：8 个模块因 `yaml` 缺失无法导入，另 1 个测试因缺少 `evaluation_data/prompt_evolution_130.jsonl` 失败。不能据此宣称完整测试通过或项目已可直接启动。
- `[INFER]` 在不重写 runtime、只做 Python/有限语言、无复杂前端的约束下，三个月业余完成可信 MVP 是 **Hard，但现实**。可复用的审查流、回放指标、Skill artifact 和 version pointer 已减少基础建设；最大工作量是 trace/version 绑定、统一 Failure、归因证据、独立评测治理和真正可靠的 promotion/rollback。

### 一句话判断

`[INFER]` 该仓库适合作为 Failure-Aware Self-Improving Code Review Agent 的基线，但不应把现有“self-evolution / canary / complete trace”直接当成已经闭环的生产能力。最合理的二次开发是补齐可观测性、失败语义、定向路由和独立验证，而不是重写已有 PR review runtime。

## 2. Repository Map

只列与 Agent runtime、PR review、self-evolution、evaluation 直接相关的路径。

| Path | Responsibility | Why it matters |
|---|---|---|
| `README.md` | 产品能力、启动/API/架构声明 | `[CODE]` 是 claim 对照基线；能力总览在 `README.md:3-22`，启动/测试在 `README.md:24-76`，架构在 `README.md:243-299`。 |
| `evoagent/__main__.py`、`evoagent/api.py` | CLI 入口、HTTP 路由、静态前端服务 | `[CODE]` `python -m evoagent` 最终进入 `api.run()`；review/webhook/evolution/deployment API 均在这里。 |
| `evoagent/config.py`、`.env.example` | 环境配置、provider、预算、评测阈值、DB/Redis/GitHub | `[CODE]` 决定 runtime 与 evolution 门禁；`.env.example:47-55` 给出评测阈值。 |
| `evoagent/service.py` | Composition root；连接 store、queue、reviewer、evolution、GitHub、repair | `[CODE]` 真正的应用层入口，决定 prompt/skill 如何进入 active runtime。 |
| `evoagent/harness.py`、`evoagent/runtime.py` | Review 状态机、节点、checkpoint、retry、cancel、timeout | `[CODE]` PR 审查从 diff 到报告的主执行骨架。 |
| `evoagent/agentic_core.py` | Lead/Worker/Critic 编排、Agent loop、并发、汇总 | `[CODE]` 多 Agent 行为和 prompt/skill/tool 的实际消费点。 |
| `evoagent/models.py` | Finding、ReviewReport、TraceEvent 等数据模型 | `[CODE]` 确定输出/trace 的真实字段；不存在名为 `ReviewTask` 或 `AgentResult` 的 domain class。 |
| `evoagent/diff_parser.py` | unified diff 解析 | `[CODE]` 只构造文件与 added lines，是 finding 定位和评测的基础。 |
| `evoagent/reviewer.py`、`evoagent/review_rules.py`、`evoagent/gates.py` | 确定性 scanner、规则、finding evidence/quality gate | `[CODE]` Agent 结果不是原样发布；会与 scanner 结果合并并过门禁。 |
| `evoagent/repository_tools.py`、`evoagent/context_manager.py`、`evoagent/memory.py` | Repository tools、上下文预算/压缩、租户记忆 | `[CODE]` 真实的工具调用和上下文管理所在。 |
| `evoagent/skills.py`、`skills/*/SKILL.md` | Agent Skill loader/catalog、内置领域 Skill | `[CODE]` Skill 是 YAML frontmatter + Markdown 指令 + 可选文本资源，不是可执行插件。当前有 9 个 Skill 目录。 |
| `evoagent/evolution.py`、`evoagent/evolution_v2.py` | Prompt 候选生成、回放、门禁、版本、rollback | `[CODE]` `llm-review` 全局 prompt evolution 主链。 |
| `evoagent/skill_evolution.py` | `SKILL.md` artifact 候选、真实 Agent graph 回放、激活 | `[CODE]` Skill evolution 是另一套独立实现。 |
| `evoagent/evaluation_harness.py` | JSONL loader、finding matcher、离线指标 | `[CODE]` 当前 100-case 数据可复现数字的底层入口。 |
| `evoagent/evaluation_v2.py` | 生产数据 readiness、详细指标、paired bootstrap、ablation | `[CODE]` 定义“真实 benchmark”要求及更完整的 cost/latency/critic 指标。 |
| `evoagent/evaluation_experiments.py`、`evoagent/evolution_proof.py` | controlled accuracy/skill experiment、prompt evolution proof | `[CODE]` 官方实验组织与缺失数据依赖所在。 |
| `evoagent/store.py`、`evoagent/postgres_store.py` | SQLite/PostgreSQL schema 与持久化 | `[CODE]` task、trace、failure、versions、eval runs、deployments 都在此；无独立 migration 目录。 |
| `evoagent/github.py` | diff 获取、评论 upsert、GitHub App/PAT 集成 | `[CODE]` 外部 PR 输入和输出边界。 |
| `evoagent/task_queue.py` | in-process/Redis Streams queue、lease、retry、DLQ | `[CODE]` webhook 异步执行依赖它。 |
| `evoagent/rollout.py` | lane assignment 与 rollout counters | `[CODE]` 有发布控制结构，但和候选 prompt 执行未接通。 |
| `evoagent/fixer.py`、`evoagent/patching.py`、`evoagent/verifier.py` | patch 生成、语法/测试验证、GitHub fix 分支 | `[CODE]` 修复是审查后的独立 API 链路，不是 Finding 生成主链的一部分。 |
| `evoagent/telemetry.py`、`evoagent/observability.py`、`evoagent/metrics.py` | Agent execution ledger、OTel span、Prometheus counters | `[CODE]` 决定现有 trace 的粒度和持久性。 |
| `tests/` | 17 个测试文件、70 个声明的 test function | `[RUNTIME]` 当前环境只成功运行其中一部分；详见第 8 节。 |
| `evaluation_data/pr_diff_100.jsonl` | 当前 100-case synthetic controlled 数据 | `[RUNTIME]` 可被 loader 读取；80 validation、20 holdout、无 train。 |
| `scripts/run_controlled_experiments.py`、`run_real_pr_benchmark.py` 等 | benchmark/experiment 命令入口 | `[CODE]` 前者要求特殊 fixture provenance；后者要求 production dataset readiness。 |
| `web/index.html`、`web/app.js`、`web/styles.css` | 静态管理台 | `[CODE]` 无单独前端构建系统；由 Python HTTP handler 直接服务。 |
| `Dockerfile`、`docker-compose.yml` | Python 3.11 镜像、Postgres/Redis/服务编排 | `[CODE]` Dockerfile 可构建服务；compose 默认值适合本地演示，不应原样视作生产安全配置。 |

### 运行契约

| 项目 | 结论 |
|---|---|
| Python 版本 | `[CODE]` README 明确要求 3.11（`README.md:24-26`）；镜像为 `python:3.11-slim`（`Dockerfile:1-11`）。没有 `python_requires` 元数据进一步约束。 |
| 核心依赖 | `[CODE]` `requirements.txt:1-6` 仅 6 组宽范围依赖：psycopg、redis、PyJWT、OpenTelemetry SDK/exporter、PyYAML；LLM HTTP 调用主要使用标准库。 |
| 启动 | `[CODE]` `python -m evoagent`；默认 `127.0.0.1:8080`。服务无模型可启动 health，但 review 会拒绝，README 此处描述与代码一致。 |
| 测试 | `[CODE]` `python -m unittest discover -s tests -v`（`README.md:72-76`）。 |
| benchmark | `[CODE]` 主要入口为 `python scripts/run_controlled_experiments.py --dataset ... --output ...`、`python scripts/run_real_pr_benchmark.py DATASET`；仓库没有统一 `make benchmark`。 |
| 打包/锁文件 | `[CODE]` 有 `requirements.txt`，无 `pyproject.toml`、`setup.py`、Pipfile、Poetry/uv/pip lock。依赖有版本范围但没有精确 lock。 |
| migrations | `[CODE]` 无 migration 框架/目录；schema 在 `store.py` 与 `postgres_store.py` 初始化时内联创建，SQLite 另有 `_ensure_column` 式增量兼容。 |
| Git 元数据 | `[RUNTIME]` 该目录不是 Git working tree，无法确认 commit、branch、未提交差异或报告与哪个 revision 对应。 |

## 3. Real PR Review Execution Flow

### 真实调用图

```text
[CODE] POST /v1/reviews                         [CODE] GitHub pull_request webhook
        ApiHandler.do_POST                              ApiHandler.do_POST
        api.py:327-367                                  api.py:368-395
              │                                                │ HMAC / age / action / dedup
              ├─ sync: ReviewService.create_review()            └─ ReviewService.handle_github_webhook()
              └─ async: ReviewService.enqueue_review()             service.py:409-451
                    service.py:275-337                                  │ fetch diff later + queue
                                  └──────────────────────┬───────────────┘
                                                         ▼
                                                ReviewHarness.run()
                                                harness.py:54-112
                                                         │ AgentRuntime state nodes
                                                         ▼
                                                _execute_review()
                                                harness.py:120-169
                                                         │ parse_unified_diff()
                                                         ▼
                                      AgenticReviewer.review_with_context()
                                      agentic_core.py:340-424
                                                         │
                      ┌──────────────────────────────────┼──────────────────────────────────┐
                      ▼                                  ▼                                  ▼
             deterministic scanners            Lead: delegations/risk             skills/tools/context
             agentic_core.py:437-482            agentic_core.py:483-556
                      │                                  │
                      └──────────────────────┬───────────┘
                                             ▼
                           Security + Correctness/Reliability Workers
                           ThreadPoolExecutor, agentic_core.py:900-1002
                                             │ high risk: Lead may request one revision
                                             ▼
                           merge candidates + FindingGate + diff-AST evidence
                           agentic_core.py:636-695,1245-1309
                                             ▼
                                  Critic blind challenge (LLM)
                                  agentic_core.py:851-898
                                             ▼
                                  Lead final index selection (LLM)
                                  agentic_core.py:1162-1182
                                             ▼
                                  Finding[] + collaboration/execution
                                             ▼
                                  ReviewReport persisted on Task
                                  models.py:64-91; harness.py:120-169
                                             │
                     ┌───────────────────────┴────────────────────────┐
                     ▼                                                ▼
             JSON/Markdown/API                              optional GitHub comment upsert
             report.py / api.py                             service.py:339-384; github.py:51-60
                                                                      │
                                                                      └─ separate /fix call may build
                                                                         verified patch/new branch
```

### 入口、输入和输出

| Step | Function/Class | Input | Output / side effect |
|---|---|---|---|
| HTTP review | `[CODE] evoagent/api.py:327-367` handler | repository、PR number、diff、mode、risk、language、async | sync 返回 task/report；async 入队后返回 task id。 |
| Webhook | `[CODE] evoagent/service.py:409-451 ReviewService.handle_github_webhook()` | signed GitHub payload + delivery id | 仅接受 opened/reopened/synchronize；claim delivery 后创建 task，diff 延迟获取并入队。 |
| Task creation | `[CODE] evoagent/service.py:275-337 create_review()/enqueue_review()` | repository/diff/PR/source/tenant/mode/root/agent/skill 等普通参数 | store 中的 task row；没有 `ReviewRequest` class，review 前强制要求配置模型。 |
| Queue worker | `[CODE] evoagent/service.py:339-407` | queue envelope/task id | 必要时 fetch diff，运行 harness，ACK/retry/DLQ；成功时可回写评论。 |
| State machine | `[CODE] evoagent/harness.py:54-112 ReviewHarness.run()` | task id + review request | `PENDING → PLANNING → EXECUTING → REVIEWING → SUCCESS/FAILED`，每阶段 checkpoint/trace。 |
| Diff parse | `[CODE] evoagent/diff_parser.py:17-50 parse_unified_diff()` | unified diff string | `ParsedDiff(files, added_lines)`；不是完整 Git AST/仓库 checkout。 |
| Agent review | `[CODE] evoagent/agentic_core.py:340-424 AgenticReviewer.review_with_context()` | parsed files + task/repo/tenant/risk/language | accepted `Finding[]`、Agent session summary、ledger summary、checkpoint payload。 |
| Report | `[CODE] evoagent/harness.py:120-169` | findings + collaboration + execution | `ReviewReport`，序列化进 task `report_json`。 |
| GitHub output | `[CODE] evoagent/github.py:51-60` | Markdown report + repo/PR | 查找已有 EvoAgent 标记评论并 update，否则 create；仅当 auto-post 开启。 |
| Fix | `[CODE] evoagent/service.py:463-480 ReviewService.create_fix()` | 已成功 task/report + GitHub token | 另行生成/验证 patch，创建新分支/提交/PR；不是 review 主链自动步骤。 |

### 关键数据结构

- `[CODE]` `evoagent/models.py:38-61 Finding`：`rule_id/severity/title/explanation/path/line/evidence/fix/test/confidence/evidence_refs/call_chain/source/gate/cwe`。
- `[CODE]` `evoagent/models.py:64-91 ReviewReport`：task、repository、PR、summary、findings、trace、collaboration、execution、timestamps。
- `[CODE]` `evoagent/models.py:94-104 TraceEvent`：只有 `step/state/message/created_at`，不是完整 LLM span。
- `[CODE]` `evoagent/models.py:6-13 TaskState`：7 个状态；`runtime.py` 在 node 间持久化 checkpoint。
- `[CODE]` `evoagent/harness.py:22-30 RuntimeState`：TypedDict，持有 request、patches、findings、report。
- `[CODE]` task、agent session、failure、candidate/evolution run 多数是数据库 row/dict/JSON；没有独立 `ReviewTask`、`AgentResult`、`Evidence`、`Candidate` domain class。`Evidence` 仅以 `Finding.evidence`、`evidence_refs`、`call_chain` 表达。

### 输出边界

- `[CODE]` Finding 必须指向 added line，模型给出的其他位置会被过滤。见 `evoagent/agentic_core.py:227-261`。
- `[CODE]` 发布前还经过 `FindingGate`：定位、必填字段、证据、置信度阈值；高风险 finding 需要更强证据并同时给 fix/test。见 `evoagent/gates.py:16-91`。
- `[CODE]` Critic 不直接发布新 finding；它只能接受/拒绝已有候选。Lead 最终也只选择候选 index。因而模型链路刻意限制 hallucinated new finding。见 `evoagent/agentic_core.py:851-898,1131-1182`。

## 4. Multi-Agent Architecture

| 问题 | 审计结论 |
|---|---|
| Lead Agent 是否真实存在 | `[CODE]` 是。固定 Lead prompt，先做 delegation/risk，必要时评估 Worker，再做 final selection；均是实际 LLM 调用。`agentic_core.py:25-43,483-556,558-635,1162-1182`。 |
| Specialist 如何选择 | `[CODE]` 启用角色由配置决定；Lead 返回 assignment、files、skills。缺失角色会被 `_normalize_delegations()` 补默认任务，所以 Lead 不负责真正的 role enable/disable。`agentic_core.py:264-316,1005-1054`。 |
| Specialist 是否并行 | `[CODE]` 是，同一进程内通过 `ThreadPoolExecutor` 并行 Security 与 Correctness/Reliability；不是独立服务或分布式 agent。`agentic_core.py:900-1002`。 |
| Critic 在哪里 | `[CODE]` 合并 scanner/worker 候选后执行；输入是去掉 source 身份的候选、diff 与审查目标。`agentic_core.py:636-695,851-898`。 |
| Critic 是什么 | `[CODE]` 一个 `allow_tools=False`、`max_steps=1` 的 LLM judge。默认解析不出明确 accept 时会拒绝。它没有独立确定性验证器。`agentic_core.py:851-898,1131-1159`。 |
| Verifier | `[CODE]` PR Finding 主链没有 verifier agent；`FindingGate` 是确定性质量 gate。修复链有编译/测试 verifier，但属于 `/fix` 后处理。 |
| Reflection | `[CODE]` 没有通用 reflection 模块。高风险路径允许 Lead 对 Worker 请求至多一次 revision，属于有限返工，不是长期/自反式 reflection。`agentic_core.py:558-635`。 |
| Red-team | `[CODE]` 没有名为 red-team 的角色或独立执行阶段。Critic 有反例挑战语义，但能力边界仍是候选 judge。 |
| Agent 如何通信 | `[CODE]` 通过普通 Python dict/session：Lead delegations → Worker inputs/results → Critic decisions → Lead indices；报告和 `agentic-lead-session` checkpoint 持久化。不是消息总线。`store.record_agent_message()` 有定义但仓库内没有调用者。 |
| 工具 | `[CODE]` Worker 的 `BoundedRole` 每步最多执行一个 tool；角色权限、Skill `allowed-tools`、runtime 可用工具取交集。普通风险 Worker 单步无工具，高风险 Worker 最多 3 步且可用工具。`agentic_core.py:76-93,111-224,900-968,1062-1095`。 |

`[RUNTIME]` fake-client 测试确认普通 review 为 5 次 LLM 调用；高风险返工测试样例为 7 次。见 `tests/test_service.py:23-47`、`tests/test_lead_worker_collaboration.py:110-142`。这确认“调用次数/并发编排存在”，不确认真实 provider 的质量。

## 5. Self-Evolution: Actual Implementation

### A. Evolution Trigger

| Signal / trigger | Status | Evidence |
|---|---|---|
| 人工 feedback | Implemented as data | `[CODE]` `/v1/tasks/{id}/feedback` 接受 `false_positive`、`missed_issue`、`bad_fix`、`accepted` 并写 `failure_cases`。`service.py:482-499`。它只记录，不自动启动 evolution。 |
| 顶层 review execution error | Implemented as data | `[CODE]` `ReviewHarness.run()` 捕获异常后写 category=`execution_error`。`harness.py:100-111`。 |
| Evaluation failure | Partial | `[CODE]` baseline/candidate 的 case errors、FP/FN 进入 evolution run/experiment metrics，但不会自动转换成 `failure_cases`。 |
| Worker 局部失败 | Partial | `[CODE]` assignment result/ledger 会记录 error；review 可继续。没有自动 failure row，因此通常不会进入 evolution。 |
| FindingGate/Critic rejection | Not implemented as trigger | `[CODE]` rejection 可出现在 report execution/collaboration，但没有统一回流。 |
| Manual API | Implemented | `[CODE]` `/v1/evolution/auto`、`/propose` 与 `/v1/skill-evolution/auto`、`/propose` 才真正触发候选流程。`api.py:481-500` 及相邻 skill-evolution routes。 |
| Scheduled task | Not implemented | `[CODE]` `continuous_eval_seconds` 只在 config 定义，未找到消费/调度调用。 |
| False negative 自动检测 | Not implemented | `[CODE]` 只有人工 `missed_issue` 或离线 gold matcher 能知道 FN；生产 review 没有 oracle。 |
| Bad repair | Implemented as manual feedback only | `[CODE]` `bad_fix` 可记录；repair 失败本身没有自动归因/演化路由。 |

### B. Failure Representation

- `[CODE]` 没有统一的 `Failure`、`FailedCase`、`EvalFailure` 或 `EvolutionSignal` 类型。
- `[CODE]` 持久层实际结构是 `failure_cases(id, task_id, category, payload_json, resolved, created_at)`；API 返回 dict。见 `evoagent/store.py:42-49,483-547`。
- `[CODE]` 人工反馈的 `payload_json` 主要承载 `finding` 与 free-form `note`；执行失败主要承载 `error`。category 是开放字符串在 store 层，service 只校验用户入口的四类。
- `[CODE]` Prompt root-cause generator 把最多 100 个 unresolved row 清洗成 JSON prompt：id/category/finding/note/task，然后让 LLM 输出 clusters 和 candidate。见 `evoagent/evolution.py:484-530`、`evoagent/evolution_v2.py:21-75`。
- `[CODE]` `accepted` 也存入名为 failure_cases 的同一表，并会进入 LLM root-cause context；这说明表的真实语义更接近“review feedback/events”，而不是严格失败集合。
- `[CODE]` 评测 case failure、gate rejection、critic rejection、worker error、repair verification failure各自处在不同 JSON/报告结构，没有公共 failure id，也没有从 failure 指回 agent step/tool/model call 的外键。

### C. Evolution Surface

| Surface | Status | What actually changes |
|---|---|---|
| Prompt | **Implemented** | `[CODE]` 保存并激活完整 prompt text；service 重建 reviewer 时作为 global prompt overlay 注入。`evolution.py:343-482`、`service.py:144-183`。 |
| Skill | **Implemented** | `[CODE]` 保存/激活完整 `SKILL.md` artifact；active DB artifact 覆盖同名磁盘 Skill，正文注入 selected worker。`skill_evolution.py:68-95,277-435`、`service.py:208-215`、`agentic_core.py:912-968`。 |
| Agent config | **Partial** | `[CODE]` structured candidate 可带 budget parameters，`AgenticReviewer._token_budget()` 会消费；lead delegation/tool policy 等字段主要被 JSON 序列化进 prompt overlay，不是独立 runtime config。`agentic_core.py:264-316`。 |
| Tool definition/schema | **Not implemented** | `[CODE]` 工具及 schema 由 `RepositoryToolSuite`/registry 代码定义，candidate 不修改实现或 schema。 |
| Context policy | **Not implemented** | `[CODE]` context budgets/compression 来自 Settings 和固定逻辑；没有 versioned candidate surface。 |
| Planner | **Partial** | `[CODE]` 可以通过 prompt additions 影响 Lead 行为，但没有独立 planner artifact/config/version。 |
| Verifier | **Not implemented** | `[CODE]` evolution 不生成或版本化 verifier；评测 matcher/gates 固定在代码中。 |
| Model/provider | **Not implemented** | `[CODE]` model 来自全局 Settings；不属于 candidate，不作为 active version 的完整快照。候选生成 telemetry 可记录 provider/model，但运行版本本身不绑定它。 |

#### “Skill” 的真实含义

- `[CODE]` `evoagent/skills.py:1-5` 明确声明 Skill 是 prompt-time capability，不是 executable reviewer。
- `[CODE]` `AgentSkill`（`skills.py:58-112`）由 `name/description/instructions/content/resource_paths/resource_contents/allowed_tools/content_sha256` 组成；loader 读取 `SKILL.md` YAML frontmatter、Markdown 正文和受路径约束的 UTF-8 文本资源（`skills.py:35-55,115-190`）。
- `[CODE]` Lead 只看到 name/description 并选择 Skill；Worker 收到正文，权限与角色工具求交集；选中资源后才注册 `read_skill_resource`。见 `agentic_core.py:523-543,912-968,1062-1095`。
- `[CODE]` 因此本项目 Skill 最准确的定义是：**可版本化、可路由、带工具权限声明和文本资源的 Markdown 审查协议/prompt package**。它不是代码、typed capability 或独立进程。

### D. Candidate Generation

#### Prompt candidate

```text
[CODE] unresolved failure_cases (max 100)
  + current active/default prompt
  + ROOT_CAUSE_PROMPT meta-prompt
  → one LLM complete_json call
  → clusters + structured candidate
  → only prompt_additions is rendered into candidate prompt
  → validation + holdout replay
  → saved prompt version + evolution_run decision
```

- `[CODE]` 一次自动调用只生成 **1 个** candidate。见 `evolution_v2.py:21-75`。
- `[CODE]` structured candidate 可含 `prompt_additions/few_shot_examples/lead_delegation_rules/tool_selection_policy/budget_parameters`；只有 prompt additions 直接改 prompt 文本，budget parameters 有程序化消费，其余主要作为 prompt context。
- `[CODE]` `parent_version` 被保存；run metrics 记录 clusters、rationale、change diff、source failure ids、provider/model、telemetry。见 `evolution.py:484-530`、`evolution_v2.py:21-75`。
- `[CODE]` dedup 只检查候选是否与**当前 active prompt**完全相同；没有对历史候选做 hash/semantic dedup。`evolution.py:343-478`。
- `[CODE]` 没有 candidate domain class；候选是字符串 + dict，版本/运行是 DB row。

#### Skill candidate

```text
[CODE] unresolved feedback rows
  → false_positive: 删除相同 rule 的 learned block
  → missed_issue: 从 finding 生成固定格式 learned block
  → one candidate SKILL.md artifact
  → actual AgenticReviewer graph replay on validation/holdout
  → save/activate or reject
```

- `[CODE]` Skill auto generation **不是 LLM root-cause generation**，而是确定性模板变换。`skill_evolution.py:183-228,377-470`。
- `[CODE]` parent artifact/version/hash 会保存；used failure ids 出现在调用结果，但 skill evolution run metrics 未像 prompt 路径那样完整持久化 source failures，来源可追溯性不一致。

### E. Prompt / Skill / Agent / Version / Evaluation 的关系

```text
[CODE] Settings(model, budgets, enabled agents)
            │
            ├─ DB active global version "llm-review" ── prompt overlay
            │
            ├─ DB active tenant skill artifacts ─┐
            │                                    ├─ AgenticReviewer
            └─ disk skills/*/SKILL.md ───────────┘      │
                                                       ├─ Lead selects skills
                                                       ├─ Workers consume instructions/tools
                                                       └─ ReviewReport

failure_cases ── manual /auto endpoint ── candidate generator
                                              │
eval_cases(validation + holdout) ─────────────┤ RegressionEvaluator
                                              │
                                              └─ version row + evolution_run + active pointer
```

`[CODE]` 名称存在历史混淆：prompt versions 使用的表/方法仍叫 `skill_versions`/`activate_skill_version`，而真正 `SKILL.md` 版本另存 `skill_artifact_versions`。后续开发若不先澄清 domain 命名，很容易把全局 prompt 与 Agent Skill 混在一起。见 `store.py:52-62,641-778`。

## 6. Evaluation / Holdout / Promotion / Rollback

### Candidate Evaluation

- `[CODE]` 在线 Prompt/Skill evolution 的 evaluation cases 来自 DB `eval_cases`；`EvolutionEngine` 在空库自动 seed 7 个内置 case：5 validation、2 holdout。见 `evolution.py:14-57,213-235`。
- `[CODE]` `RegressionEvaluator` 做 canonical CWE/path/line 近似匹配，计算 precision、recall、F1、severity accuracy、high-severity recall、clean accuracy、success rate，并以 `0.65*F1 + 0.15*severity + 0.20*clean`（按可用项）形成 score，再乘 success rate。见 `evolution.py:62-210`。
- `[CODE]` 在线默认每个 split 最多取 5 个 case，validation 至少 3、holdout 至少 2；按 store 返回顺序截取，没有随机/分层抽样。`evolution.py:313-332`、`.env.example:47-52`。
- `[CODE]` baseline 和 candidate 都在 validation 与 holdout 上执行；holdout aggregate 与门禁结果会被返回/持久化，只隐藏逐 case 细节。每次 proposal 都再次用同一 holdout 决策。因此这个 holdout 是**反复使用的 adaptive promotion set**，不是独立、只在最终报告开启一次的 holdout。
- `[CODE]` `evaluation_experiments.py:449-545` 的单次 controlled Skill experiment 比在线引擎更严格：每轮只用 validation 选版本，选择完才评 holdout。`[UNKNOWN]` 仓库没有跨多次实验的访问控制/数据版本 registry，无法证明人员不会反复查看同一 holdout 后继续调参。
- `[CODE]` 在线 promotion 没有 statistical test、bootstrap、置信区间、latency/cost gate 或 model-call budget gate。paired bootstrap 存在于 `evaluation_v2.py:596-633` 的 offline experiment/ablation 路径，不被在线 promotion 调用。
- `[CODE]` safety gate 仅做 prompt 非空/长度上限、禁用若干危险短语，并要求出现 diff/severity/fix/test/json 词；不是安全 benchmark。见 `evolution.py:299-311`。

### Promotion 条件

#### Prompt

- `[CODE]` 直接 `propose`：无 eval error、validation score 至少提高 `min_improvement`、受保护 validation/holdout 指标均不超过允许回退，才自动激活；否则 rejected/deferred。真实判断见 `evolution.py:343-478,599-609`。
- `[CODE]` `auto_propose` + LLM root-cause generator 强制 `activation_policy="shadow"`，即门禁通过也只保存为 `shadow_ready`，不会设置 active。见 `evolution.py:484-530`。
- `[CODE]` 手工 prompt version activation API 最终可激活任意已保存版本，store 不检查该版本曾是 rejected/deferred/shadow_ready；可以绕过评测 gate。见 `store.py:641-683` 与对应 API route。

#### Skill

- `[CODE]` candidate validation score 必须提高，validation/holdout protected metrics 都不能超阈值回退，且回放无错误，才 decision=`activated` 并切 active pointer。见 `skill_evolution.py:277-375`。
- `[CODE]` Skill 手工 activation 只允许本来已 active 或某个 run decision 为 `activated` 的版本，约束强于 prompt version。

### Version Registry

| Field | Prompt version | Skill artifact version |
|---|---|---|
| version_id/version | `[CODE]` 有整数 version | `[CODE]` 有整数 version |
| parent_version | `[CODE]` 有 | `[CODE]` 有 |
| prompt/artifact | `[CODE]` 完整 prompt text | `[CODE]` 完整 `SKILL.md` artifact + hash |
| model | `[CODE]` 不在版本 row；部分 generation metrics 有 | `[CODE]` 不在版本 row |
| runtime config | `[CODE]` 不完整；structured details 在 run metrics | `[CODE]` 不完整 |
| created_from_failures | `[CODE]` prompt generation run metrics 有 source ids | `[CODE]` skill 路径持久化不完整 |
| metrics | `[CODE]` score 在 version，详细 metrics 在 evolution_run | `[CODE]` score + skill_evolution_run metrics |
| status | `[CODE]` 只有 active bool + run decision 字符串，无严格 lifecycle state machine | `[CODE]` 同左 |
| timestamp | `[CODE]` 有 | `[CODE]` 有 |
| tenant scope | `[CODE]` prompt version 是全局 | `[CODE]` skill artifact 是 tenant-scoped |

`[CODE]` 没有 `created → validating → active/rejected → rolled_back` 的原子状态机；version row 与 run decision 分离，也没有 immutable bundle 将 prompt + skills + model + budgets + tool schemas 绑定成一次可复现 release。

### Rollback 到底恢复什么

- `[CODE]` `EvolutionEngine.rollback()` 与 `SkillEvolutionEngine.rollback()` 只切换 DB active pointer。见 `evolution.py:480-482`、`skill_evolution.py:473-474`。
- `[CODE]` 通过 API 执行 rollback/activate 后，handler 调 `ReviewService.reload_skills()`；service 会重建 AgenticReviewer 与 ReviewHarness，因此**之后新进入的任务**会读取旧 prompt/skill。见 `api.py:540-560`、`service.py:144-215`。
- `[CODE]` 若直接在内部调用 engine.rollback，而不经 API reload，当前内存 reviewer 不会自动刷新。
- `[INFER]` 已经开始的任务持有旧 reviewer/runtime reference，不能保证在 rollback 瞬间切换；当前没有事务/版本 pinning 明确其语义。
- `[UNKNOWN]` 没有 integration test 证明“review 使用候选 → rollback → 下一 review 一定使用旧 artifact”，所以不能声称 rollback 已经端到端可靠。

### Canary / Shadow 的真实状态

- `[CODE]` `rollout.py:10-65` 有 stable/candidate version、canary ratio、lane assignment、promotion counters；task metadata 也记录 release_lane/shadow。`service.py:241-259`。
- `[CODE]` `ReviewService._build_agentic_reviewer()` 只读 DB active `llm-review` pointer，不按 task lane 选择 stable/candidate version。`service.py:144-183`。
- `[CODE]` `ReleaseManager.observe_shadow()` 在仓库中没有调用者；服务成功/失败只调用 lane observation。`service.py:301-310,371-383`。
- `[CODE]` rollout promotion 更新 deployment row 的 stable pointer，不会同步激活 prompt version；runtime 又不消费 deployment pointer。
- `[CODE]` SQLite store 有 shadow observation 方法，但 PostgreSQL store 缺少对应方法，接口能力不对称。
- `[INFER]` 因此当前 canary/shadow 更接近“发布控制数据结构和计数骨架”，不是能把 candidate 与 stable 同时执行、比较并可靠提升的闭环。

## 7. Trace & Observability

### 一次 Agent Run 实际记录什么

| Field | Recorded? | Where | Persistent? | Enough for failure attribution? |
|---|---|---|---|---|
| task/input diff | Yes | `[CODE]` task row/input payload；`store.py` | Yes | 部分；有输入但无 git commit/checkout snapshot。 |
| system prompt | No snapshot | `[CODE]` 固定 prompt 在 `agentic_core.py:25-74` | No | No；以后代码/active pointer 变化后无法恢复当时精确 prompt。 |
| agent prompt / actual managed context | Partial | `[CODE]` session 保存 assignment/部分结构；context manager 保存统计/hash | Partial | No；完整发送内容未保存。 |
| plan/delegation | Yes | `[CODE]` agent session collaboration/checkpoint | Yes | Yes，足够知道 Lead 给了谁什么任务。 |
| step/state | Yes, coarse | `[CODE]` `TraceEvent` + AgentRuntime checkpoints | Yes | 只够阶段级。 |
| tool call/arguments | Yes | `[CODE]` `ExecutionLedger` events | Yes via report/checkpoint | 基本够定位调用。 |
| tool result | Truncated | `[CODE]` ledger 只存 string preview，约 1000 chars；evidence ref 也有截断 | Yes, partial | No；关键证据可能丢失，序列化也不稳定。 |
| observation | Partial | `[CODE]` BoundedRole action/ledger、finding evidence refs | Partial | No；working observation 全量是瞬时数据。 |
| retrieved memory/evidence | Partial | `[CODE]` 记录 recall count/scope 与 selected evidence | Partial | No；没有完整 recalled items 和排序分数快照。 |
| raw LLM response | No | `[CODE]` 只保留解析后的 delegation/findings/critic decision/final indices | No | No；无法审计 parser 前后的损失。 |
| critic result | Yes, normalized | `[CODE]` session collaboration | Yes | 基本够做 accept/reject 归因，不够复盘 raw reasoning。 |
| verifier result | N/A in review | `[CODE]` review 无 verifier；repair 有验证结果 | repair response only/路径相关 | 对 review attribution 不够。 |
| final answer/report | Yes | `[CODE]` `ReviewReport` | Yes | Yes。 |
| error | Yes, uneven | `[CODE]` task error、TraceEvent、worker result、ledger、failure_case | Yes | 顶层错误够；worker/gate/eval 错误没有统一关联。 |
| latency | Yes | `[CODE]` ledger model/tool duration + task timestamps | Yes | Yes。 |
| tokens | Yes | `[CODE]` model-call ledger summary | Yes | Yes。 |
| model/provider | Yes per call | `[CODE]` ledger | Yes | Yes，但没有和 version bundle 固化。 |
| prompt/skill/version | No immutable binding | `[CODE]` collaboration 可列 skill 名；task 有 lane | No完整快照 | No；这是归因的关键缺口。 |

### 能否重建 decision trajectory？

`[CODE]` **只能重建简化轨迹**：runtime state → Lead delegation → Worker 结构化 findings/tool event previews → Critic decisions → Lead final indices → published findings。`agentic_core.py:1229-1242` 会把 session 与 ledger summary 写入 checkpoint/report。

`[CODE]` **不能精确重放模型当时看到和回答的内容**。至少缺：

1. 每次调用的完整 system/user prompt 与压缩后 context；
2. raw response 及 JSON parse/normalization 前后值；
3. 未截断、结构化的 tool observations；
4. 每步所用 prompt version、Skill artifact hash、model/config/tool-schema bundle；
5. failure/feedback 到具体 agent step、candidate finding、critic decision 的稳定关联 id；
6. gate rejection 的统一 reason taxonomy 与 source step。

`[CODE]` OpenTelemetry 只包 runtime/review span 与有限 attributes（`observability.py:12-48`）；Prometheus counters 在进程内，重启后不持久。告警 row 可持久，但告警通知相关配置未见实际 webhook/SMTP sender。它们不能弥补上述语义 trace 缺口。

## 8. Benchmark & Test Reality

### 当前 benchmark 数据

`[RUNTIME]` 对 `evaluation_data/pr_diff_100.jsonl` 做只读解析得到：

| Property | Actual |
|---|---|
| Case count | 100 |
| Split | validation 80 / holdout 20 / train 0 |
| Repository count | 10；repo 1-8 仅 validation，9-10 仅 holdout |
| Risk / clean | 40 risk / 60 clean |
| Expected findings | 40 |
| Severity | medium 16 / high 15 / low 5 / critical 4 |
| Rule/category | 21 个 rule id/category |
| Label grain | PR-level `risk` + finding-level rule/category/severity/path/line/evidence；不是只有 PR label |
| Human label | `should_comment` 缺失 |
| Source | 全部 `synthetic-controlled`，generator=`evoagent-e2e-v1` |
| Auto-fixable | true 26 / false 14（在 expected finding 上） |

`[RUNTIME]` loader 计算的 dataset fingerprint 为 `88831bb...`（本报告保留短前缀；脚本可重新计算完整 SHA）。

### 代码真实计算的 metrics

- `[CODE]` `evaluation_harness.py:244-416`：Precision、Recall、F1、Severity Accuracy、High-risk Recall、Clean PR Accuracy、Execution Success、Safe Fix、E2E Success；matcher 以 canonical CWE/rule、path、行号容差和 severity 为基础（`evaluation_harness.py:93-152`）。
- `[CODE]` `evaluation_v2.py:433-584`：在 production harness 中进一步记录 invalid comments/PR、exact-line/evidence/human acceptance、latency、tokens、cost、role/model calls、critic acceptance、revision、failure rate。
- `[CODE]` `evaluation_v2.py:596-633`：有 paired bootstrap 95% interval，但用于 offline fair comparison，不用于在线 promotion。
- `[CODE]` 在线 `RegressionEvaluator` 的指标更少，不做 cost/latency/statistical comparison。

### 数字追踪

用当前 JSONL 和底层 harness 直接回放：

| Reviewer | TP / FP / FN | Precision | Recall / F1 | High-risk Recall | Clean Accuracy |
|---|---:|---:|---:|---:|---:|
| `[RUNTIME]` `LocalRuleReviewer` | 25 / 5 / 15 | 0.8333 | 0.6250 / **0.7143** | **0.8421** | **0.9167** |
| `[RUNTIME]` `LocalRuleReviewer + ContextRuleReviewer` | 33 / 7 / 7 | 0.8250 | 0.8250 / **0.8250** | **0.9474** | **0.9167** |

- `[RUNTIME]` 因而问题中举例的 `F1 71.4 → 82.5`、`High-risk recall 84.2 → 94.7`、`Clean PR accuracy 91.7` 与当前 synthetic dataset 上两套确定性 reviewer 的结果精确对应。
- `[CODE]` README 当前没有写这组三个具体数字，也没有截图文件；`scripts/run_controlled_experiments.py:31-93` 会输出实验叙事，但其中若干“critic/multi/evolved delta=0”“candidate rejected”等文字是固定模板，不是全部由 runtime condition 动态推导。
- `[CODE]` controlled adapter 要求 100 case、10 repo 且 `source.kind=offline-fixture`，然后重划 60/20/20；当前文件是 `synthetic-controlled`，不满足。`evaluation_experiments.py:49-77`。
- `[CODE]` production readiness 要求至少 300 个 `public-github-pr/private-historical-pr`、三 split、repo disjoint、human `should_comment`；当前文件也不满足。`evaluation_v2.py:68-120`。
- `[CODE]` `evolution_proof.py` 默认依赖 `evaluation_data/prompt_evolution_130.jsonl`，该文件不存在。`evolution_proof.py:30-36`。

**分类：`Partially reproducible`。** `[RUNTIME]` 核心数字能从当前数据和规则直接重现；`[CODE]` 官方 controlled/production 命令、数据 provenance、LLM provider/version/config 和已提交实验输出不足以让第三方一键复现“完整 Agent/evolution 效果”。

### Tests & Runtime Reality

| Test command | Passed | Failed / Error | Skipped / not executed | Blocker |
|---|---:|---:|---:|---|
| `[RUNTIME] python -m unittest discover -s tests -v` | 0 | command exit 127 | 全部 | 当前机器没有 `python` 命令。 |
| `[RUNTIME] PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -v` | 21 | 9 unittest error entries | 8 个 import-failed 模块中的 48 个声明测试未收集 | Python 3.9.6；PyYAML 缺失导致 8 个模块 ImportError；另 1 项缺 `prompt_evolution_130.jsonl`。 |
| `[RUNTIME]` 独立运行不依赖 YAML 的测试子集 | 19 | 0 | 其余未运行 | 覆盖 diff parser、GitHub HMAC、harness、reviewer、runtime/context/memory、Lead/Worker 协作。 |
| `[RUNTIME]` AST parse 全部 Python 文件 | 65 files | 0 syntax errors | N/A | 只证明当前解释器可解析，不证明 3.11 行为/依赖/集成。 |
| `[RUNTIME]` 底层 JSONL loader | 100 cases | 0 | N/A | 成功。 |
| `[RUNTIME] python3 -B scripts/run_real_pr_benchmark.py ...` | 0 | exit 1 | benchmark 未开始 | 导入阶段缺 PyYAML；之后仍会被 production dataset readiness 拦截。 |

`[RUNTIME]` 本次没有安装依赖、没有调用 LLM/GitHub/付费 API、没有启动 Postgres/Redis、没有写生产 DB。`[UNKNOWN]` 因而 PostgreSQL、Redis、GitHub、真实 LLM、OTel exporter、修复分支等外部集成只做了静态审计，未做 live integration verification。

## 9. README Claims vs Code Reality

| README claim | Classification | Audit |
|---|---|---|
| unified diff → structured finding/fix/test | Supported | `[CODE]` diff parser、Finding、report、gates 均存在。 |
| GitHub webhook/HMAC/comment upsert | Supported by code | `[CODE]` action、HMAC、replay window、delivery claim、fetch diff、comment update/create 均有实现；`[UNKNOWN]` 未调真实 GitHub。 |
| Lead/Security/Correctness/Critic multi-agent | Supported | `[CODE][RUNTIME]` 真实多次调用、双 Worker 并发、Critic gate；普通 5-call 测试成立。 |
| SQLite task/trace/report | Supported | `[CODE]` schema 与读写存在；trace 不是完整 decision trace。 |
| checkpoint/resume/budget/runtime | Supported | `[CODE]` node runtime、checkpoint、timeout、retry、cancel 与 token/time budget 有实现。 |
| Tool Registry/schema/structured observation | Supported, bounded | `[CODE]` registry 与 schema 校验存在；完整 observation 没有持久化。 |
| PostgreSQL/Redis production mode | Partial | `[CODE]` 两套实现广泛存在；Postgres 缺部分 shadow observation 方法，且本次无 live runtime evidence。 |
| failure feedback → prompt eval/version/rollback | Partial but real | `[CODE]` 数据、候选、回放、版本、pointer 均存在；触发是手工，failure 不统一，prompt auto 不会直接 active。 |
| dynamic Skill manifest | Supported | `[CODE]` reload、frontmatter、正文/资源、allowed-tools、DB artifact override 均存在。 |
| Skill signature verification | Not supported | `[CODE]` 只见内容 hash/路径/manifest validation，未见密码学签名或 trust root 验证。 |
| isolated process sandbox for Skill | Not supported | `[CODE]` Skill 是注入同一 Agent 上下文的 Markdown；无 Skill 独立进程。 |
| repair compile/test gate and Draft PR fix | Partial/Supported by code | `[CODE]` patch 与验证/GitHub 分支代码存在；验证可靠性依赖配置 test command、仓库 checkout 与外部权限，未 live test。 |
| canary / shadow traffic | Half-built | `[CODE]` 有 lane/schema/counters/API，但 candidate version 未被 review runtime 加载，shadow observation 无调用者，promotion pointer 不驱动 active prompt。 |
| OpenTelemetry/Prometheus/persistent alerts | Partial | `[CODE]` span、metrics endpoint、alert DB 存在；Prometheus 非持久，未见配置字段对应的外部告警发送。 |
| local provider keeps deterministic rule-only review (`.env.example:16-18`) | Conflicts with code | `[CODE]` review service 在无 LLM 时调用 `_require_agentic_model()` 拒绝审查；确定性 rules 只在 agentic review 内合并。health-only 启动可以。 |
| “完整执行轨迹”语义 | Overstated | `[CODE]` ledger 很有价值，但缺完整 prompt/context/raw response/full tool result/version snapshot，无法精确重建。 |

`[CODE]` 另有一个展示层不一致：agent session 使用 `candidate_findings_before_critic`，而 Markdown renderer 读取 `candidate_findings`（`report.py:59-61`），因此报告中的候选数量可能显示 0，即使内部实际有候选。

## 10. Reusable Components

### Already reusable

- `[CODE]` `Finding` / `ReviewReport` / canonical rule identity：可作为审查输出契约。
- `[CODE]` `parse_unified_diff()` / `ParsedDiff`：可用于 MVP 的 unified-diff added-line 基线；若只做有限语言，不必先替换。
- `[CODE]` `FindingGate`：可复用为格式、证据、置信度和高风险发布门禁。
- `[CODE]` `AgentRuntime` + checkpoint/cancel/retry/budget：已有可靠的状态机骨架。
- `[CODE]` `ExecutionLedger`：可作为更完整 Run Trace 的底座，而不是推倒重写。
- `[CODE]` Lead/双 Worker/Critic 固定编排：足够作为简历项目 baseline。
- `[CODE]` `RepositoryToolSuite` 与 tool schema/permissions：足以支持有限的 evidence gathering。
- `[CODE]` Skill loader、artifact validation、allowed-tools、active DB override：非常适合 Targeted Skill Evolution。
- `[CODE]` JSONL loader、finding matcher、离线 metrics、paired bootstrap：可直接用于独立 evaluation 的底层实现。
- `[CODE]` GitHub webhook/client、queue、task store、Markdown report：可保持外围产品闭环。

### Extend

- `[INFER]` Trace：给每次 model/tool/gate/critic event 增加稳定 id、完整结构化输入输出引用、version bundle 与 failure link。
- `[INFER]` Failure representation：在现有 `failure_cases` 上增加统一 taxonomy、source stage、expected/actual、evidence、attribution status，而非另造完全独立系统。
- `[INFER]` Candidate/evolution run：补 immutable dataset fingerprint、source failure ids、model/config/tool schema hashes、generation seed/reason。
- `[INFER]` Evaluation：保留 matcher/metrics，增加真正 train/evolution/validation/final-holdout 治理和一次性报告规则。
- `[INFER]` Promotion/rollback：保留 active pointer，但需让 runtime 按明确 version bundle 加载，并写端到端 integration test。
- `[INFER]` Benchmark：当前 synthetic 100 cases 可作为开发 regression set，不能作为最终外部有效性证据。

### Refactor

- `[CODE]` `evolution.py` 与 `skill_evolution.py` 重复 candidate replay/gating/version logic，且激活策略不一致，应抽出共同 evaluation/promotion contract。
- `[CODE]` prompt 版本仍叫 `skill_versions`，而真正 Skill 另有表；domain 命名需要澄清。
- `[CODE]` `agentic_core.py` 超过千行，角色 prompt、orchestration、parsing、gating、skill/tool routing 混合；要做精确 attribution，需要先拆出明确 stage hooks，而非重写 runtime。
- `[CODE]` session/candidate/failure 多为自由 dict/JSON，schema drift 已造成 renderer 字段不一致；关键结构应 typed/versioned。
- `[CODE]` SQLite/PostgreSQL store 重复实现且功能已有偏差；应建立接口契约/contract tests。
- `[CODE]` rollout/deployment 与 active version 加载是两套未连接的 pointer，应统一，否则 shadow/promotion 结论不可信。

## 11. Architectural Gaps

| Module | Classification | Gap |
|---|---|---|
| Trace | **Extend** | `[CODE]` 有 ledger/checkpoint，但缺 exact prompts/raw outputs/full observations/version binding。 |
| Failure representation | **Refactor/Extend** | `[CODE]` failure_cases 太泛，accepted 与 error 共表，其他失败散落。 |
| Failure taxonomy | **Missing** | `[CODE]` 没有稳定的 agent-stage/root-cause taxonomy。 |
| Attribution | **Missing** | `[CODE]` 没有 failure → trace step → evolution surface 的算法或人工确认状态。 |
| Evolution routing | **Missing/Partial** | `[CODE]` 目前所有 unresolved rows可一起进入 prompt generator；Skill 路径只按两类模板处理，没有归因驱动的 surface selector。 |
| Candidate generation | **Extend** | `[CODE]` prompt 单 candidate LLM、Skill 单 deterministic candidate；缺历史 dedup、candidate pool、统一 provenance。 |
| Evaluation | **Extend/Refactor** | `[CODE]` 有真实回放与指标，但在线 holdout 反复使用，online/offline 口径不一，无 promotion statistics/cost gate。 |
| Promotion | **Refactor** | `[CODE]` prompt direct/auto/Skill/manual 规则不一致；manual prompt activation 可绕 gate；shadow promotion不驱动 runtime。 |
| Versioning | **Extend** | `[CODE]` 有 parent/active/score/artifact，但没有完整 release bundle 和 lifecycle。 |
| Rollback | **Extend** | `[CODE]` pointer 可切换，API 可 reload；缺 in-flight 语义、原子性与 end-to-end test。 |
| Benchmark | **Extend** | `[RUNTIME]` 100-case synthetic regression set可用；真实 PR、human label、>=300 production set 缺失。 |

其他关键阻碍：

- `[CODE]` `parse_unified_diff()` 只保留文件名和新增行；跨文件、删除语义、完整 AST/调用图需要 tools 或外部 checkout，归因时不能假设模型拥有完整 repo context。
- `[CODE]` 普通风险 Worker 只有一次无工具调用；其 evidence 很大程度来自 diff 和 scanner。若将失败归因成“模型推理差”，可能实际是“证据接口未开放”。
- `[CODE]` Worker failure 不一定使整个 task 失败，容易形成 silent degradation；Failure Attribution 必须读取 per-role status，而不能只看 TaskState。
- `[CODE]` online eval 与 production benchmark 的 dataset schema/readiness 不同，必须避免把小型 regression gate 和最终 benchmark 混用。

## 12. Failure Attribution Feasibility

### 判断

`[INFER]` **现实，且与当前代码结构相容；但必须先扩 trace/version binding，不能直接在现有 `failure_cases.note` 上让 LLM 猜根因。**

### 最可能的插入位置

1. `[CODE]` 生产 review 完成/异常：`evoagent/harness.py:54-112 ReviewHarness.run()`。这里同时拥有 TaskState、checkpoint、顶层异常和最终 report，可创建 run-level failure signal。
2. `[CODE]` finding 候选生命周期：`evoagent/agentic_core.py:636-695`。这里能关联 scanner/Worker candidate、FindingGate、Critic、Lead final selection，是区分 generation failure、evidence failure、critic rejection、aggregation loss 的最佳位置。
3. `[CODE]` 人工标签入口：`evoagent/service.py:482-499 record_feedback()`。这里已有 FP/FN/bad-fix/accepted，可把人类反馈绑定 finding id/trace event/version。
4. `[CODE]` 离线 gold 对比：`evoagent/evaluation_harness.py:281-416` 与 `evaluation_v2.py:433-584`。这里能稳定产生 FP/FN/severity/location/evidence failure，而不是依赖 free-form note。
5. `[CODE]` evolution 入口：`evoagent/evolution.py:484-530` 和 `skill_evolution.py:377-435`。这里应消费“已归因、可训练”的 failure，而不是所有 unresolved row。
6. `[CODE]` runtime 版本加载：`evoagent/service.py:144-215`。这里最适合为每个 task 固化 prompt version + skill hashes + model/config，保证归因与回放指向同一版本。

### 为什么可行

- `[CODE]` 已有候选来源、Critic decision、Lead final selection、gate result、model/tool ledger、feedback 与 gold matcher，大部分 attribution observation 已存在，只是没有统一关联。
- `[CODE]` 已有两个有限 evolution surface：global prompt 和 selected `SKILL.md`。MVP 可把 taxonomy 路由到这两类，不必做代码/工具自修改。
- `[CODE]` 已有 replay evaluator、version parent、active pointer，可承载“定向修改 → validation → promote/rollback”。

### 必须避免的过度结论

- `[UNKNOWN]` 现有 trace 无法证明某次 FN 是 prompt、Skill、上下文压缩、tool permission、provider 漂移还是 gold label 错误。
- `[INFER]` v1 attribution 应允许 `unknown/insufficient_evidence` 和人工确认，不能强制每个 failure 选一个单一根因。
- `[INFER]` 在没有独立最终 holdout 前，即使 validation 上升也只能叫 regression improvement，不能叫 self-improvement 已泛化。

## 13. Three-Month Feasibility

### MVP

范围：Baseline reproduction + Failure Taxonomy + Failure Attribution v1 + Targeted Prompt/Skill Evolution + Independent Evaluation + Promotion/Rollback + Benchmark。

**难度：`Hard`，但在严格收敛范围后可完成。**

原因：

- `[CODE]` 有利条件：review runtime、agent roles、feedback API、Skill packaging、replay metrics、version pointer、rollback API、synthetic regression set 已存在，基础设施不是从零开始。
- `[RUNTIME]` 不利条件：当前环境/依赖/缺失数据让官方 baseline 不能一键复现；第一阶段必须先建立可重复 Python 3.11 环境和诚实的 benchmark 命令。
- `[CODE]` 最大技术风险不是 candidate prompt 生成，而是 trace/version/failure 对齐、holdout 治理和 promotion 真正作用到 runtime。
- `[INFER]` 一个人业余三个月可把目标限定为：Python/unified diff；现有四角色流程；只演化 global prompt 与 1-2 个 Skill；规则/LLM 混合 attribution v1；manual approval promotion；一个 synthetic regression set + 一个严格隔离的小型 final set；rollback integration test。

建议三个月内做：

- `[INFER]` 固化 Python 3.11/依赖与一键 baseline；明确当前 100 cases 只是 synthetic regression。
- `[INFER]` 增加 immutable run/version snapshot 和关键 stage event ids。
- `[INFER]` 定义有限 failure taxonomy，并支持 unknown/manual confirmation。
- `[INFER]` 只做 Prompt vs Skill 的 targeted routing，每次 1-3 个可解释 candidate 即可。
- `[INFER]` 将 train/evolution、validation、final holdout 分离；最终 holdout 不用于循环调参。
- `[INFER]` 采用明确 manual promotion + 原子 active pointer + “下一任务使用旧/新版本”的集成测试。
- `[INFER]` 交付可复现 benchmark JSON/Markdown，披露数据 provenance、模型、成本和限制。

三个月内暂不做：

- `[INFER]` 不重写完整 Agent Runtime/RAG/MCP，不做多 Git provider/全语言，不做复杂前端。
- `[INFER]` 不做任意 tool schema/code self-modification，不声称 autonomous production evolution。
- `[INFER]` 不把 canary/shadow UI/计数骨架包装成真实线上实验，除非先完成 version routing 和双跑观测。

### Stretch

| Stretch item | Difficulty | Judgment |
|---|---|---|
| Tool Interface Evolution | **Unrealistic**（与上述完整 MVP 同期） | `[INFER]` 工具 schema、权限、执行安全、兼容性和独立评测都需新机制。若只做“从已有工具集合选择策略”则降为 Hard，但不应称 Tool Interface Evolution。 |
| Evidence-based runtime verification | **Hard** | `[INFER]` 利用现有 RepoToolSuite/FindingGate 做有限语言、有限规则的证据核验可作为后半程 stretch；完整跨文件语义 verifier 超出范围。 |
| Human-feedback evolution | **Moderate** | `[CODE]` ingestion 与 task/tenant 关联已存在；主要补 finding identity、去重、标签质量、归因确认和 provenance。若加入真实用户平台/主动学习则会升为 Hard。 |

## 14. Most Important Code Locations

| Location | Symbol | Why |
|---|---|---|
| `evoagent/api.py:327-395` | HTTP POST routing | review 与 webhook 外部入口。 |
| `evoagent/service.py:275-451` | `ReviewService` review/queue/webhook methods | task 创建、异步执行、GitHub 回写主边界。 |
| `evoagent/harness.py:54-169` | `ReviewHarness.run()` / `_execute_review()` | runtime 状态、错误、checkpoint、report 主链。 |
| `evoagent/runtime.py:123-219` | `AgentRuntime.run()` | node budget/retry/checkpoint/cancel。 |
| `evoagent/agentic_core.py:111-261` | `BoundedRole` | 单 Agent model/tool loop 与 finding 解析。 |
| `evoagent/agentic_core.py:340-738` | `AgenticReviewer.review_with_context()` | 完整多 Agent orchestration。 |
| `evoagent/agentic_core.py:851-898` | Critic execution | LLM judge 的真实能力边界。 |
| `evoagent/agentic_core.py:900-1002` | Worker execution | Specialist 并发、风险级别、tool/revision 行为。 |
| `evoagent/agentic_core.py:1229-1309` | session persistence/merge/evidence | trace checkpoint、dedup、AST evidence。 |
| `evoagent/gates.py:16-91` | `FindingGate` | 发布前证据/质量 gate。 |
| `evoagent/models.py:38-104` | `Finding` / `ReviewReport` / `TraceEvent` | 核心输出与 trace schema。 |
| `evoagent/skills.py:35-190` | `AgentSkill` / loaders | Skill 的真实定义和加载安全边界。 |
| `evoagent/evolution.py:14-210` | built-in cases / `RegressionEvaluator` | 在线评测数据与 scoring。 |
| `evoagent/evolution.py:343-609` | propose/auto/rollback/gates | Prompt evolution、promotion、holdout 的核心事实。 |
| `evoagent/evolution_v2.py:10-75` | `RootCauseEvolutionGenerator` | failure → LLM structured candidate。 |
| `evoagent/skill_evolution.py:183-470` | Skill candidate/propose/auto | deterministic Skill evolution 与回放。 |
| `evoagent/service.py:144-215` | reviewer/skill rebuild | active prompt/Skill 真正进入 runtime 的位置。 |
| `evoagent/store.py:42-134,483-778` | failure/version/evolution schemas/methods | failure 与版本 registry 的事实来源。 |
| `evoagent/rollout.py:6-65` | `ReleaseManager` | canary/shadow 骨架及未接通边界。 |
| `evoagent/evaluation_harness.py:93-152,244-416` | matcher/run/metrics | 100-case 数字的实际算法。 |
| `evoagent/evaluation_v2.py:68-120,433-633` | readiness/production metrics/bootstrap | 独立 benchmark 的更严格标准。 |
| `evoagent/evaluation_experiments.py:49-77,449-545` | fixture adapter/Skill experiment | 当前数据与官方脚本不兼容、单次 holdout 选择逻辑。 |

## 15. Open Questions / Unknowns

1. `[UNKNOWN]` 当前目录没有 Git 元数据，无法确认这是 upstream 哪个 commit、是否遗漏 Git LFS/未跟踪 benchmark 产物、或文件是否被手工修改。
2. `[UNKNOWN]` 缺少 Python 3.11 + 已安装依赖的本地环境，本次不能确认完整 70-test suite 在声明环境中是否通过。
3. `[UNKNOWN]` 未提供真实 LLM provider/key，不能确认各 provider 对 JSON contract、并发、token usage、重试的真实兼容性和结果质量。
4. `[UNKNOWN]` 未连接 PostgreSQL/Redis，不能确认 schema 初始化、lease、DLQ、并发 worker 与 SQLite 行为一致；静态上已观察到 rollout method parity 缺口。
5. `[UNKNOWN]` 未连接 GitHub，不能确认私有仓库 diff、comment upsert、App installation token、repair branch 的权限/幂等边界。
6. `[UNKNOWN]` `prompt_evolution_130.jsonl` 与已提交 benchmark outputs 不存在；无法确认作者曾运行何种 prompt-evolution experiment。
7. `[UNKNOWN]` 当前 100-case 文件是谁生成/审阅、是否在规则开发中被反复使用、是否有独立人类复核，仓库没有 provenance 记录可证明。
8. `[UNKNOWN]` 生产中 holdout 是否被人工多次查看/调参没有访问日志与治理机制；代码只能证明在线 gate 会反复使用它。
9. `[UNKNOWN]` rollback 对 in-flight task 的预期语义没有文档/测试；新任务经 API reload 的行为可从代码推断，不能扩展到并发原子性保证。
10. `[UNKNOWN]` “签名验证、隔离进程 sandbox、完整 shadow traffic”若存在于仓库外服务，本地代码没有接口或证据可确认。

# CHATGPT_HANDOFF

对象：`/Users/bytedance/Downloads/EvoAgent(3)`；无 `.git`，commit `[UNKNOWN]`。它是 Python 3.11 的真实 PR reviewer：`api.py:327-395` 接 API/webhook，`service.py:275-451` 建任务、取 diff、可回写 GitHub，`harness.py:54-169` 跑状态机并保存报告。

`[CODE]` 主链：`parse_unified_diff()` → scanners → Lead → Security 与 Correctness/Reliability Worker → FindingGate → Critic → Lead final。双 Worker 在 `agentic_core.py:900-1002` 线程并行；普通任务 5 次 LLM，高风险最多返工一轮。缺失角色会被 `1005-1054` 补默认 assignment，Lead 不能真正关闭 Specialist。Critic（`851-898`）是无工具 LLM judge，只接受/拒绝候选；无 review verifier、red-team 或通用 reflection。

`[CODE]` Self-Evolution 有两条。Prompt：`evolution.py:343-609` 将 failures + active prompt 交给 `evolution_v2.py:10-75`，一次生成一个 candidate；只有 prompt additions 和 budget 参数有直接 runtime 效果。validation/holdout 回放后保存版本；direct propose 可激活，LLM auto 只到 `shadow_ready`。Skill：`skill_evolution.py:183-470`；Skill 是 `skills.py:35-190` 的 Markdown 指令/资源/allowed-tools prompt package，不是代码插件。auto 以确定性模板增删 learned block，通过门禁可激活。

`[CODE]` Failure 在 `store.py:42-49,483-547` 仅为 `category + payload_json + resolved`。`service.py:482-499` 人工记录 FP/FN/bad-fix/accepted；`harness.py:100-111` 自动记录顶层 execution_error。Worker 错误、Gate/Critic rejection、eval FP/FN、repair failure 没有统一回流；Evolution 只由手工 API 触发，无 scheduler。

`[CODE]` 版本有 parent/artifact/score/active/metrics，但无完整 immutable release bundle。Prompt 手工激活可绕 gate。rollback 切 DB pointer，API 再 rebuild reviewer；in-flight 原子性 `[UNKNOWN]`，无集成测试。Canary/shadow 是半成品：`rollout.py:10-65` 有 lane/counter，但 runtime 不按 lane 加载 candidate，`observe_shadow()` 无调用者。

`[CODE]` Trace 有状态、delegation、结构化 Agent 结果、model/tool ledger 和报告；缺完整 prompt/context/raw response/tool result 及 version snapshot，只能重建阶段骨架。

`[RUNTIME]` `pr_diff_100.jsonl` 有 100 个 synthetic-controlled case：80 validation/20 holdout、40 risk/60 clean。LocalRuleReviewer 得 F1 .7143/high-risk .8421/clean .9167；加 ContextRuleReviewer 得 F1 .8250/high-risk .9474/clean .9167。题示数字可复现，但只是合成数据上的确定性规则。官方 controlled adapter 要求 `offline-fixture`，production harness 要求 >=300 真实/历史 PR、三 split、human label，故结论是 `Partially reproducible`。

`[RUNTIME]` 本机 Python 3.9.6，`python` 不存在，PyYAML 等未装。fallback discovery：21 pass、9 error entry；8 个模块因 yaml 缺失未导入，另 1 项缺 `prompt_evolution_130.jsonl`。LLM/GitHub/Postgres/Redis 均未实测，状态 `[UNKNOWN]`。

Failure Attribution 插点：run failure 在 `harness.py:54-112`；candidate/gate/critic/aggregation 在 `agentic_core.py:636-695`；人工标签在 `service.py:482-499`；gold FP/FN 在 `evaluation_harness.py:281-416`；目标演化在 `evolution.py:484-530` / `skill_evolution.py:377-435`；version snapshot 在 `service.py:144-215`。先补 event id、version snapshot、统一 taxonomy 与 unknown/manual-confirmed 状态，再做归因。

三个月单人业余 MVP：`[INFER] Hard but feasible`。保留 runtime/GitHub/前端，仅支持 Python/unified diff，只演化 global prompt 与 1-2 个 Skill；做可复现 baseline、taxonomy、attribution v1、定向路由、独立 validation/final holdout、manual promotion、rollback integration test和诚实 benchmark。暂不做完整 Tool Interface Evolution、全语言/多 provider、重写 runtime、复杂前端或生产自治。Stretch：Human-feedback evolution Moderate；有限 evidence verification Hard；完整 Tool Interface Evolution 与 MVP 同期 Unrealistic。
