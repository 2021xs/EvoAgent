# Trace V2 and Baseline Freeze Plan

调查日期：2026-09-16  
对象：`/Users/bytedance/Downloads/EvoAgent(3)`  
约束：本轮只读调查；除本计划文档外，不修改源码、依赖、配置、数据库或评测数据。

证据标签沿用上一轮：`[CODE]` 代码可直接确认；`[RUNTIME]` 本机实际执行确认；`[INFER]` 基于代码的设计判断；`[UNKNOWN]` 当前证据不足。

## 1. Baseline Freeze Definition

### 1.1 当前状态

**`BASELINE_FREEZE = NOT_READY`。**

当前不能称为真正冻结，原因是：

- `[RUNTIME]` 本机只有 `/usr/bin/python3` 3.9.6，没有 `python` 或 `python3.11`。
- `[CODE]` 项目要求 Python 3.11（`README.md:24-26`、`Dockerfile:1`），但没有 `python_requires` 或工具链文件固定到某个 3.11 patch release。
- `[CODE]` `requirements.txt:1-6` 是版本范围而非精确 lock/hashes；同一命令在不同日期可能解析到不同 wheel。
- `[RUNTIME]` 当前 declared third-party import targets `yaml`、`psycopg`、`redis`、`opentelemetry.sdk`、OTLP exporter 均缺失；`jwt` 也缺失，但当前认证实现没有使用 PyJWT。
- `[CODE]` `evaluation_data/pr_diff_100.jsonl` 的 provenance 是 `synthetic-controlled`，而 controlled adapter 和测试硬要求 `offline-fixture`（`evaluation_experiments.py:49-77`、`tests/test_evaluation_harness.py:16-33`）。
- `[CODE][RUNTIME]` `evaluation_data/prompt_evolution_130.jsonl` 不存在，但 `evolution_proof.py:28-41` 与 `tests/test_evolution_proof.py:13-30` 将其作为必需输入。
- `[UNKNOWN]` 没有固定的真实 LLM provider/model endpoint、可用凭据和已提交结果，因此 multi-agent LLM baseline 尚未建立。

### 1.2 Python 3.11 最小运行环境

#### requirements.txt 是否足够

- `[CODE]` 对仓库中可见的 Python 第三方 imports，`requirements.txt` **覆盖是足够的**：PyYAML 支持 Skill/frontmatter；psycopg 支持 PostgreSQL；redis 支持 Redis Streams；OpenTelemetry 两项支持 tracing/exporter。
- `[CODE]` `PyJWT[crypto]` 当前没有被代码 import；`AuthManager` 在 `auth.py:112-129` 自行实现 HS256 token。它是多余声明，不是运行 blocker。
- `[CODE]` 核心 review、HTTP、LLM transport、SQLite、diff、测试框架主要使用标准库。
- `[CODE]` 完整工具行为还可能依赖系统二进制 `git/semgrep/bandit/eslint/mypy/pyright`，见 `repository_tools.py:244-294`；它们不属于最小 core/test/deterministic baseline，且缺失时 scanner 会报告 unavailable，不应塞进最小 Python lock。
- `[INFER]` 所以 `requirements.txt` 足以表达“可安装依赖集合”，但**不足以表达可重复环境**。Baseline Freeze 需要另存一个由 Python 3.11 clean environment 实际解析得到的精确版本 + hash lock；本轮不生成。

#### 当前实际缺失

| Dependency/import | Declared | Current host | Needed for |
|---|---|---|---|
| `yaml` / PyYAML | `[CODE] requirements.txt:6` | `[RUNTIME] missing` | Skill loader；缺失会阻断 8 个 test modules 和 benchmark imports。 |
| `psycopg` | `[CODE] requirements.txt:1` | `[RUNTIME] missing` | 仅 PostgreSQL mode；SQLite baseline 不需要。 |
| `redis` | `[CODE] requirements.txt:2` | `[RUNTIME] missing` | 仅 Redis queue mode；in-memory queue baseline 不需要。 |
| `opentelemetry.sdk` | `[CODE] requirements.txt:4` | `[RUNTIME] missing` | 可选 tracing；代码会降级为 no-op（`observability.py:12-27`）。 |
| OTLP HTTP exporter | `[CODE] requirements.txt:5` | `[RUNTIME] missing` | 仅配置 endpoint 时需要。 |
| `jwt` / PyJWT | `[CODE] requirements.txt:3` | `[RUNTIME] missing` | 当前代码未使用，不影响现有 auth tests。 |

#### Python 3.11 compatibility

- `[RUNTIME]` 40 个 package Python 文件、17 个 test files 在 Python 3.9.6 下可被发现/部分执行；上一轮对 65 个 Python 文件做 AST parse 无语法错误。
- `[CODE]` 未发现 `distutils`、`imp`、`asyncore`、`cgi`、`collections.Mapping`、`inspect.getargspec` 等 3.11 移除/常见不兼容 API。
- `[INFER]` 能在 3.9 解析且未使用上述 removed APIs，说明源码与 3.11 **高度可能兼容**。
- `[UNKNOWN]` 当前没有 Python 3.11 executable，不能把静态判断升级为 `[RUNTIME]`；完整 3.11 test run 是 Freeze 的硬条件。

#### 理论上的干净环境与测试命令

首次解析只用于产生 lock，不是最终可重复安装：

```bash
python3.11 -m venv .venv-baseline
.venv-baseline/bin/python -m pip install -r requirements.txt
```

真正冻结后应使用精确 lock（当前不存在）：

```bash
.venv-baseline/bin/python -m pip install --require-hashes -r requirements-lock.txt
PYTHONDONTWRITEBYTECODE=1 .venv-baseline/bin/python -m unittest discover -s tests -v
```

`[CODE]` 当前 Dockerfile 只复制 `evoagent/`、`web/`、`skills/`，不复制 `tests/`、`scripts/`、`evaluation_data/`（`Dockerfile:5-11`），所以不能直接把现有 runtime image 当成 test/benchmark image。为保持范围小，Baseline Freeze 优先使用独立 Python 3.11 venv，而非改造部署镜像。

### 1.3 当前可记录的身份信息

以下是本次只读计算所得的 provisional fingerprints；真正 Freeze 仍应使用 Git commit 或不可变 source archive digest：

| Artifact | SHA-256 |
|---|---|
| `[RUNTIME]` 86-file workspace fingerprint（排除 `.env`、DB、pyc、两份审计报告） | `e36c384837b4b27e5fb33099325a54a2b0073c9be1c060e6d5ef123206f022e8` |
| `[RUNTIME]` raw `evaluation_data/pr_diff_100.jsonl` | `e3cf61546e0f554c045a79949202532046f7e936d8bbb1e2f5d0de2b79634d74` |
| `[RUNTIME]` logical dataset fingerprint from `dataset_fingerprint()` | `88831bb19264f9fc15433de7801b623aad38b80076f5d5b085d0299fd40cc115` |
| `[RUNTIME]` `requirements.txt` | `573fe69eea000791899a1566c5815f790fde380fb73af163b384112d90447f22` |

### 1.4 Minimal `BASELINE_FREEZE` checklist

只有以下全部满足，才写入 `BASELINE_FREEZE=true`：

- [ ] `[INFER]` 固定 source identity：优先 Git commit；若仍无 Git，使用完整 archive SHA-256，并保存生成规则。
- [ ] `[INFER]` 固定 CPython **具体 3.11 patch version**、平台/架构、pip version；若用镜像，固定 image digest，而不是浮动 `python:3.11-slim`。
- [ ] `[INFER]` 从 clean Python 3.11 环境生成并提交 `requirements-lock.txt`，含所有 transitive versions 与 hashes；重复安装使用 `--require-hashes`。
- [ ] `[CODE]` 明确 `pr_diff_100.jsonl` 为 **development regression set**，保留真实 `synthetic-controlled` provenance；不要把它重命名成真实/最终科学 benchmark。
- [ ] `[INFER]` 统一 controlled adapter/tests 与上述 provenance。推荐让代码明确接受“synthetic development fixture”，而不是为过测试篡改 metadata。
- [ ] `[INFER]` 恢复并固定 `prompt_evolution_130.jsonl` 的真实原始文件/hash；若无法恢复，则在后续实现阶段用新、可审计的小 fixture 重写 proof contract，不能静默 skip。
- [ ] `[RUNTIME]` clean Python 3.11 上 70/70 declared tests green；不允许 import error、missing dataset 或未记录 quarantine。
- [ ] `[RUNTIME]` deterministic baseline 以固定数据 hash 运行，关键 metrics 与 frozen golden JSON 精确相同。
- [ ] `[RUNTIME]` multi-agent LLM baseline 固定 provider、endpoint origin、model identifier、temperature、预算、请求参数和运行日期；保存完整结果、token/cost/latency。LLM 输出不要求 bitwise identical，但协议必须可重复。
- [ ] `[RUNTIME]` self-evolution baseline 固定输入 feedback/splits/candidate policy/threshold/seed，保存 baseline/candidate/version/gate/holdout 输出。
- [ ] `[INFER]` 生成一个小型 `baseline_manifest.json`：source、Python、lock、dataset hashes、commands、environment variable names、model config、seeds、outputs SHA；绝不保存 API key。
- [ ] `[INFER]` 输出目录每次新建且不复用 DB；prompt proof 本身也要求 fresh output DB（`scripts/run_prompt_evolution_proof.py:20-28`）。

这份 checklist 不要求 OTel backend、PostgreSQL、Redis、GitHub live call、frontend build 或 canary/shadow；它们与 Failure Attribution baseline 无直接关系。

## 2. Test Matrix

### 2.1 分类

一个文件可能覆盖多个领域；“P0”表示后续 Trace/Failure Attribution 二次开发必须保持 green。

| Test file | Count | Primary category | P0? | What it protects |
|---|---:|---|---|---|
| `tests/test_config.py` | 2 | core runtime | Yes | `[CODE]` dotenv parsing 与 env precedence；version snapshot 读取配置的基础。 |
| `tests/test_diff_parser.py` | 1 | core runtime | Yes | `[CODE]` added-line path/line identity；finding lifecycle 的定位基础。 |
| `tests/test_reviewer.py` | 1 | core runtime | Yes | `[CODE]` deterministic finding 只落新增行。 |
| `tests/test_harness.py` | 2 | core runtime / storage | **Critical** | `[CODE]` task state、failure、trace/checkpoint 基本行为。 |
| `tests/test_runtime_memory_context.py` | 7 | core runtime / storage | **Critical** | `[CODE]` checkpoint restore、ToolRegistry validation、working memory/tool observation。 |
| `tests/test_context_manager.py` | 4 | core runtime / multi-agent | **Critical** | `[CODE]` 实际 model context 的压缩、摘要、memory 注入；Trace V2 必须不改变语义。 |
| `tests/test_service.py` | 3 | multi-agent / integration | **Critical** | `[CODE]` 真实 ReviewService path、5-call topology、feedback persistence。 |
| `tests/test_lead_worker_collaboration.py` | 3 | multi-agent | **Critical** | `[CODE]` Lead delegation/revision/final、resume 不重放、gate memory。 |
| `tests/test_agentic_evaluation.py` | 2 | multi-agent / evaluation | Yes | `[CODE]` role topology、公平实验臂、非生产数据 claim gate。 |
| `tests/test_phases_0_5.py` | 6 | multi-agent / evolution / repair / evaluation | Yes | `[CODE]` model-required、四角色、structured evolution、patch/verifier、real-data gate。 |
| `tests/test_advanced.py` | 12 | evolution / storage / service | **Critical** | `[CODE]` feedback tenant scope、prompt gate、holdout regression、dedup、immutable eval cases。 |
| `tests/test_skill_evolution.py` | 7 | evolution / multi-agent / storage | **Critical** | `[CODE]` Skill parse/select/replay/activation/tenant override。 |
| `tests/test_evolution_proof.py` | 1 | evolution / evaluation | Yes, currently blocked | `[CODE]` feedback → prompt version → validation/holdout → activation proof；缺 130-case artifact。 |
| `tests/test_evaluation_harness.py` | 6 | evaluation | **Critical** | `[CODE]` dataset contract、one-to-one matcher、CWE identity、fingerprint。 |
| `tests/test_evaluation_experiments.py` | 4 | evaluation / evolution | **Critical** | `[CODE]` 60/20/20 repo split、metric contract、five-arm ablation、locked holdout。 |
| `tests/test_production_features.py` | 8 | storage / GitHub-adjacent integration | Yes | `[CODE]` auth/tenant、webhook idempotency、resume、queue/DLQ、release counters、repair verifier。注意它不证明真实 canary candidate routing。 |
| `tests/test_github.py` | 1 | GitHub/integration | Yes | `[CODE]` webhook HMAC；没有真实 GitHub network test。 |

### 2.2 当前 runtime 状态

`[RUNTIME]` 本轮重新执行：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -v
```

结果：21 pass，9 unittest error entries。8 个模块在 import 阶段因 `yaml` 缺失失败：`test_advanced`、`test_agentic_evaluation`、`test_evaluation_experiments`、`test_evaluation_harness`、`test_phases_0_5`、`test_production_features`、`test_service`、`test_skill_evolution`；`test_evolution_proof` 因缺 `prompt_evolution_130.jsonl` 失败。其余被收集的 21 项通过。

`[CODE][INFER]` 即使安装 PyYAML，当前数据 provenance 仍会导致至少三处冲突：

- `tests/test_evaluation_harness.py:30-33` 断言 source 只能是 `offline-fixture`；
- `tests/test_evaluation_experiments.py:121-136` 会进入 `prepare_controlled_experiment_cases()`，它拒绝 `synthetic-controlled`；
- `tests/test_evaluation_experiments.py:138-148` 要求 `dataset_contract_passed=True`，当前该字段因 provenance 为 false。

因此“安装 requirements 后全绿”不是已证事实。Freeze 前必须先统一真实 provenance contract。

### 2.3 后续改 Trace V2 时的 minimum green gate

每次提交至少运行以下逻辑组；最终仍以 70/70 为准：

1. `[INFER]` runtime/trace：`test_harness`、`test_runtime_memory_context`、`test_context_manager`。
2. `[INFER]` agent lifecycle：`test_service`、`test_lead_worker_collaboration`、`test_agentic_evaluation`。
3. `[INFER]` ID/metrics correctness：`test_diff_parser`、`test_evaluation_harness`、`test_evaluation_experiments`。
4. `[INFER]` evolution compatibility：`test_advanced`、`test_skill_evolution`、`test_evolution_proof`。
5. `[INFER]` persistence/integration：`test_production_features`、`test_github`，并新增 SQLite/PostgreSQL store API parity tests。

## 3. Benchmark Matrix

### 3.1 三种 baseline 必须分开命名

| Baseline | Actual entry | Minimal command | Dataset | Model/API | Metrics | Executable now? | Blocker |
|---|---|---|---|---|---|---|---|
| Deterministic rule baseline | `[CODE] evaluation_experiments.py:214-246 AccuracyExperimentSuite.run_controlled()`；其 reviewer 是 `CompositeReviewer([LocalRuleReviewer(), ContextRuleReviewer()])` | `PYTHONDONTWRITEBYTECODE=1 .venv-baseline/bin/python scripts/run_accuracy_experiment.py --output output/baseline/deterministic.json` | `pr_diff_100.jsonl` | None | Precision/Recall/F1/high-risk recall/severity/clean/exact-line/evidence/invalid comments/safe-fix/E2E/execution；见 `evaluation_experiments.py:33-40` | `[RUNTIME]` 当前否 | PyYAML 未装；装后可执行，但 `dataset_contract_passed` 会因 `synthetic-controlled` 为 false。 |
| Current multi-agent LLM reviewer baseline | `[CODE] scripts/run_agentic_evaluation.py:33-108` → `FairAblationSuite` → `ProductArmReviewer("full-agentic")` → `AgenticReviewer` | 见下方固定命令 | `pr_diff_100.jsonl` 仅作 dev regression；以后另用真实 final set | Required | 上述准确率指标 + role calls、tokens、latency、cost、critic acceptance/rejection、revision；paired bootstrap 比较 | `[RUNTIME]` 当前否 | PyYAML、provider/base URL/API key/model 均缺；结果存在 provider nondeterminism。 |
| Self-evolution: prompt proof | `[CODE] scripts/run_prompt_evolution_proof.py` → `run_prompt_evolution_proof()` | `.../python scripts/run_prompt_evolution_proof.py --dataset evaluation_data/prompt_evolution_130.jsonl --output-dir output/baseline/prompt-evolution` | `prompt_evolution_130.jsonl` | None；使用 deterministic `PromptPolicyReviewer` | baseline/candidate validation/holdout 的 score/P/R/F1/severity/high-risk/clean/success + version/gates | `[RUNTIME]` 否 | 数据文件缺失。它证明的是 controlled behavior change，不是 LLM improvement。 |
| Self-evolution: Agent Skill | `[CODE] scripts/run_skill_evolution_experiment.py:34-142` → `SkillEvolutionExperimentSuite` | `.../python scripts/run_skill_evolution_experiment.py DATASET ...` | 必须有 train/validation/holdout；current 100 只有 80/20 | Required | static/evolved/random-feedback arms、protected metrics、holdout、bootstrap、release gate | `[RUNTIME]` 当前否 | model/API 缺失；`--adapt-controlled-100` 又被当前 source-kind mismatch 阻断。 |

#### Deterministic baseline 的命名边界

- `[CODE]` `LocalRuleReviewer` 单独是最小 rule anchor；仓库没有专用 CLI。上一轮 `[RUNTIME]` 得到 F1 `0.7143`。
- `[CODE]` 现有 `run_accuracy_experiment.py` 冻结的是“当前完整 deterministic rules”，即 Local + Context composite；上一轮 `[RUNTIME]` 得到 F1 `0.8250`。
- `[INFER]` `BASELINE_FREEZE` 应把 composite 作为正式 deterministic baseline，并把 Local-only 数字作为 component diagnostic，避免同一个“rule baseline”名称指向两套 reviewer。

#### Multi-agent LLM development command

```bash
EVOAGENT_LLM_BASE_URL='<fixed endpoint>' \
EVOAGENT_LLM_API_KEY='<secret; never write to manifest>' \
EVOAGENT_LLM_MODEL='<fixed model id>' \
EVOAGENT_LLM_PROVIDER='<provider>' \
.venv-baseline/bin/python scripts/run_agentic_evaluation.py \
  evaluation_data/pr_diff_100.jsonl \
  --allow-non-production-data \
  --token-budget 12000 \
  --time-budget 240 \
  --bootstrap-iterations 2000 \
  --bootstrap-seed 20260819 \
  --output output/baseline/agentic-evaluation.json
```

`[CODE]` 对当前文件不要加 `--adapt-controlled-100`，因为该 flag 会调用只接受 `offline-fixture` 的 adapter。`--allow-non-production-data` 会运行 harness，但保持 production claim gate 关闭。

`[CODE]` 这个 CLI 使用真实 `AgenticReviewer`，但外层是 evaluation 专用 `ProductArmReviewer` 和 `_EvaluationTaskStore`（`evaluation_v2.py:255-366`），不是批量调用 `ReviewService`。所以它是**审查算法 baseline**，不是 service/GitHub/queue E2E baseline。

### 3.2 `pr_diff_100.jsonl` 的正式定位

**`[CODE][RUNTIME] development regression set, not final scientific benchmark`。**

它适合：

- 检查确定性 rules、finding matcher 和 metrics 是否回退；
- 调试 multi-agent topology、Trace V2 完整性、candidate lifecycle；
- 做本地 smoke/ablation，production claim 始终关闭。

它不适合：

- 宣称真实 GitHub PR 泛化效果；
- 作为反复调参后仍“独立”的 final holdout；
- 证明 LLM、Critic 或 self-evolution 的科学有效性；
- 同时充当 evolution feedback/train、validation 和最终 benchmark。

`[CODE]` production readiness 自己也要求至少 300 个 public/historical PR、人类 `should_comment`、三 split、repo disjoint（`evaluation_v2.py:68-120`）。Freeze 应保留这一 gate，不降低它来适配当前 100 cases。

## 4. Current Attribution-Relevant Event Flow

注意真实顺序：最终 `FindingGate` 在 Critic 和 Lead final **之后**，不是之前。

| Order | Location / function | Current data | Missing attribution data |
|---:|---|---|---|
| 1 | `[CODE] service.py:241-259 ReviewService._create_task()` | task id、tenant/repo/PR、diff bytes/hash、lane、mode、requested agents/skills | 实际 prompt version、model/config、resolved Skill hashes、tool schemas；没有 run-start event。 |
| 2 | `[CODE] harness.py:54-112 ReviewHarness.run()` | coarse task trace、checkpoints、success/cancel/failure；顶层 error 写 failure_case | 没有 run id/attempt event graph；failure_case 没有 source event id/version snapshot。 |
| 3 | `[CODE] runtime.py:140-218 AgentRuntime.execute()` | 定义 `RuntimeEvent`：node start/complete/fail/retry/restore/budget | `ReviewHarness.run()` 没传 `event_sink`，所以这些 rich runtime events 当前被丢弃。 |
| 4 | `[CODE] harness.py:120-140 _planning()/_executing()` | parsed files/added lines，状态转移，调用 reviewer | parse rejection 只有顶层 error；无 input artifact ref、attempt/parent relation。 |
| 5 | `[CODE] agentic_core.py:340-388 _review_with_context()` | mode、task input、repo-root availability、enabled roles、resolved skills、memory recall count | 没有实际 runtime version snapshot；memory event没有具体 memory ids/content refs。 |
| 6 | `[CODE] agentic_core.py:437-481 _scan()` | scanner tool logs、finding count、scanner evidence refs、merge 后 findings | 每个 scanner candidate 没有 candidate id；scanner name/ordinal 到 surviving finding 的 lineage 丢失。 |
| 7 | `[CODE] agentic_core.py:523-556 _agentic()` + `820-849 _run_lead()` | Lead activation/completion、normalized assignments、risk、objective、worker、files、skills | Lead model input/output未保存；assignment event没有 parent model-call id；默认补 assignment 与 Lead 原始选择未区分成 reason code。 |
| 8 | `[CODE] agentic_core.py:130-196 BoundedRole.run()` | role start、context token stats、action kind/tool/reason、finish/budget | 无 assignment/run correlation；无 exact rendered system/user、raw/parsed output、stable model-call event id。 |
| 9 | `[CODE] llm.py:25-79 JsonChatClient.complete_json()` | 发送 model/temp=0/messages/JSON format/max tokens；ledger 仅记录 role/provider/model/usage/latency/ok/error | exact request、API-visible raw content、parsed object、response model/id/fingerprint 均丢失；JSON parse error 时最关键 raw content也丢失。 |
| 10 | `[CODE] repository_tools.py:345-430 RepositoryToolSuite.registry()` | schema-validated args、structured evidence result；ledger 保存 args、1000-char string preview、latency/error | preview 非结构化且易截断；无 parent decision event。动态 `read_skill_resource` 不走此 wrapper，只剩 tool_observation name/ok。 |
| 11 | `[CODE] agentic_core.py:227-261 _parse_findings()` | raw dict → typed Finding，验证 added-line location，attach referenced observations | invalid path/line、bad types等被静默 drop；没有 raw candidate id、parse reason或 raw→typed lineage。 |
| 12 | `[CODE] agentic_core.py:900-1002 _run_pending_assignments()` | worker status、assignment/run id、revision round、typed findings/error；checkpoint after result | worker start没有 assignment event；finding没有 stable id；raw output/parse rejections不可见。 |
| 13 | `[CODE] agentic_core.py:1124-1128,1245-1256 _session_candidates()/_merge()` | 以 path+line+canonical identity 去重，保留最高 confidence；排序稳定 | 被替换 candidates、tie/retained reason、contributors 全部丢失；后续只靠易变 index。 |
| 14 | `[CODE] agentic_core.py:571-634` high-risk assessment/revision | Lead assessment、revision request/result、assignment/round/status | request 到原 candidate/evidence gap 的稳定关联缺失。 |
| 15 | `[CODE] agentic_core.py:636-660,851-898,1131-1159` Critic | blinded candidate index、normalized accepted/objections、confidence adjustment | 只用 list index；无 candidate/finding id、raw response。Critic rejection不会过滤 candidate，只供 final Lead 参考。 |
| 16 | `[CODE] agentic_core.py:661-695,1162-1182` Lead final | accepted indices、confidence adjustments、accepted findings | 未选 candidate没有显式 reason；indices 在 merge后才有意义，六周后难稳定 join。 |
| 17 | `[CODE] agentic_core.py:389-424` + `gates.py:25-91 FindingGate.apply()` | 每个被选 Finding 的 gate dict；rejected list含 rule/path/line/reasons；aggregate counters | rejected item无 finding id；reason是自由字符串；无法稳定关联 Critic/Lead/candidate source。 |
| 18 | `[CODE] harness.py:142-169 _reviewing()` | collaboration、execution、rejected findings、report | report finding无 lifecycle id；没有逐 finding published event或 version snapshot id。 |
| 19 | `[CODE] agentic_core.py:1229-1242 _save_lead_session()` | session + ledger summary 写 `agentic-lead-session` checkpoint | checkpoint是恢复状态，不是规范 append-only event log；并发/重试的因果父子关系需推断。 |

## 5. Trace V2 Schema

### 5.1 设计目标

`[INFER]` Trace V2 只解决四件事：稳定关联 Failure；识别 first divergence；区分 Lead/Worker/Critic/Gate；六周后知道输入、输出与版本。它不保存 hidden reasoning。

### 5.2 Minimal event envelope

```json
{
  "schema_version": 2,
  "event_id": "evt_...",
  "run_id": "<task_id>",
  "parent_event_id": "evt_... or null",
  "sequence": 17,
  "stage": "worker",
  "actor": "security",
  "event_type": "candidate_generated",
  "subject_id": "cand_...",
  "outcome": "completed",
  "reason_codes": [],
  "input_refs": ["sha256:..."],
  "output_refs": ["sha256:..."],
  "data": {},
  "error": null,
  "created_at": "2026-09-16T...Z"
}
```

字段语义：

| Field | Why it is required |
|---|---|
| `event_id` | `[INFER]` Failure 和后续事件的稳定外键；不能再靠 list index。 |
| `run_id` | `[INFER]` 直接复用 task id；resume 是同一逻辑 run，attempt 放在 `data`，避免新增 run registry。 |
| `parent_event_id` | `[INFER]` 表达因果而非仅时间：assignment → worker model call → tool → candidate。并行 Worker 不依赖全局时间猜父子。 |
| `sequence` | `[INFER]` 人类阅读和同一 run 的确定排序；由统一 sink 分配，不能使用 role-local sequence。 |
| `stage` | `[INFER]` 小枚举：`runtime/context/scanner/lead/worker/critic/merge/gate/report`。 |
| `actor` | `[INFER]` `runtime/lead/security/correctness-reliability/critic/finding-gate/...`。 |
| `event_type` | `[INFER]` 事实动作，如 model_call、tool_call、candidate_generated、decision、published。 |
| `subject_id` | `[INFER]` assignment/candidate/finding/version snapshot 的稳定 id，支持直接查生命周期。 |
| `outcome` | `[INFER]` 小枚举：started/completed/accepted/rejected/failed/skipped。 |
| `reason_codes` | `[INFER]` 稳定机器标签；free-form explanation 放 `data`，归因不解析英文句子。 |
| `input_refs/output_refs` | `[INFER]` 指向同一 event payload 中的 content hash或既有 task/checkpoint artifact；能验内容未变。 |
| `data` | `[INFER]` stage-specific JSON；避免为每种事件加 DB column。 |
| `error` | `[INFER]` `{type,message}` 或 null；message 有上限，不含 secret。 |

最小 reason code 集：

```text
parse.invalid_location
parse.invalid_shape
merge.lower_confidence
merge.duplicate_identity
critic.accepted
critic.rejected
critic.no_decision
lead.selected
lead.not_selected
gate.format
gate.evidence
gate.confidence
gate.release
report.published
runtime.error
```

`[INFER]` `data` 里可以保留人类可读 objections/reason，但 attribution 的 grouping 与 first-divergence 判断只依赖枚举 code。

### 5.3 Reuse plan

- `[INFER]` 保留 `TraceEvent` 作为 task 状态时间线，不把它膨胀成 Agent event。
- `[INFER]` 扩展 `ExecutionLedger` 为 Trace V2 producer：构造时带 `run_id` 与 optional event sink；现有 model/tool summary 保持兼容。
- `[INFER]` 把 `AgentRuntime.execute(event_sink=...)` 接到同一 sink，复用已经定义但当前未使用的 `RuntimeEvent`。
- `[INFER]` collaboration/session 继续负责 resume；只在其中保存 `event_id/finding_id` 引用，不复制完整 event log。
- `[INFER]` Store 增加 append/list run event；不替换现有 trace/checkpoint/report。

## 6. Candidate/Finding Lifecycle Trace

### 6.1 Stable identifiers

需要两个层次，不能只用一个 index：

- `candidate_id`：一次具体来源输出。建议由 `run_id + origin(scanner/assignment/revision) + ordinal + canonical raw JSON hash` 确定性生成，resume 后相同输出得到相同 id。
- `finding_id`：merge 后的逻辑问题。建议由 `run_id + normalized path + line + canonical_identity(rule_id,cwe)` 生成；同一 scanner/worker 的重复 candidate 汇入同一个 finding id。

`[INFER]` 为最小改动，可给 `Finding` 增加 optional `finding_id` 与 `candidate_ids`，默认空值以兼容现有构造器；无需引入完整 Candidate domain framework。

### 6.2 Actual-order lifecycle

```text
scanner/worker raw output
  └─ candidate_generated(candidate_id, origin_event_id)
       ├─ parse_rejected(candidate_id, reason_code)
       └─ candidate_parsed(candidate_id, provisional finding key)
            ↓
merge/dedup
  └─ finding_merged(finding_id, candidate_ids, retained_candidate_id, reason)
            ↓
Critic decision(finding_id, accepted/rejected/objections)
            ↓  Critic does not directly remove it
Lead final decision(finding_id, selected/not_selected)
            ↓
FindingGate decision(finding_id, accepted/rejected, reason_codes)
            ↓
report.published(finding_id) or terminal non-publication event
```

### 6.3 回答“expected finding 为什么没发布”

将 gold expected finding 与同一 run 的 candidates/finding IDs 做现有 canonical matcher 后，first divergence 规则是：

1. 没有匹配 `candidate_generated`：`generation_miss`。
2. 有 raw candidate，但 `parse_rejected`：`candidate_parse_failure`。
3. merge 后逻辑 finding 存在；被低 confidence duplicate 替换不算消失，但 lineage 显示 retained source。
4. Critic rejected 但 Lead 后来选择：不是 terminal failure；记录 `critic_overridden`。
5. Lead 未选择：`lead_not_selected`。
6. Lead 选择但 FindingGate 拒绝：按 `gate.*` 得到 first terminal divergence。
7. 已发布但 line/severity/category 与 gold 不符：`published_mismatch`，由 evaluation matcher产生，不伪装成生成失败。

`[CODE]` 当前 `_apply_critic()` 不过滤 candidates（`agentic_core.py:1131-1159`），所以 Trace V2 必须分别记录 Critic opinion 与 Lead terminal selection，不能把 `critic.accepted=false` 直接解释成未发布原因。

### 6.4 最小事件集

每个 review 至少需要：

- 1 个 `run_version_snapshot`；
- 每次 model call 1 个 completed/failed event；
- 每次 tool call 1 个 completed/failed event；
- 每个 raw candidate 1 个 generated + 0/1 个 parse rejection；
- 每次 merge 1 个映射 event；
- 每个 merged finding 1 个 Critic decision、1 个 Lead decision、若 selected 则 1 个 Gate decision；
- 每个发布 finding 1 个 published event；
- 1 个 run completed/failed event。

不需要记录每个 Python function enter/exit。

## 7. Model/Tool Snapshot Policy

### 7.1 Model-call snapshot

真实调用点是 `BoundedRole.run()`（`agentic_core.py:130-224`）构造 managed context，然后 `JsonChatClient.complete_json()`（`llm.py:25-79`）发送 OpenAI-compatible request。

| Data | Policy | Reason |
|---|---|---|
| Rendered system prompt | **Save full, once per unique SHA; event stores hash/ref** | 硬编码 role prompt + active overlay + Skill instruction都会变化；只有 hash 无法复盘。 |
| Exact rendered user input | **Save full canonical JSON + SHA** | 必须知道压缩、memory、tools、observations 后模型实际看到了什么，才能定位 context divergence。 |
| Context hash/stats | **Save inline** | source diff hash、managed-context hash、token estimate、dropped/summarized counts便于快速筛查。 |
| Raw assistant content | **Save full API-visible content + SHA** | 区分 model produced bad JSON 与 parser/normalizer丢失；应在 `json.loads` 前捕获。只保存 message content，不保存 provider hidden reasoning。 |
| Parsed response | **Save full canonical JSON + SHA** | 区分 raw→parsed→normalized 各阶段。 |
| Model/provider | **Save inline** | provider、requested model、response model（若返回）、endpoint origin、temperature、response_format、max_tokens、timeout。 |
| Usage/cost/latency | **Save inline** | 当前 ledger 已有大部分；保留 provider-reported usage。 |
| Response metadata | **Small allowlist** | response id、system fingerprint/finish reason 若 provider 提供；不保存完整 HTTP body。 |
| Secrets/headers | **Never save** | API key、Authorization、private extra headers与归因无关。 |
| Hidden chain-of-thought | **Never request/save** | 不需要且不应成为 attribution 依赖。 |
| Logprobs/token stream | **Not needed v1** | 当前 JSON client不请求；会显著增加数据量。 |

`[INFER]` 本地三个月项目可把 system/user/raw/parsed 放在 event `data` 中并带 SHA；不必先建通用 blob service。若用户代码敏感，应加明确 retention/redaction 开关；关闭 full-content 时必须在 trace 标记 `content_available=false`，不能假装仍可做同等可信归因。

### 7.2 Tool-call snapshot

| Field | Minimal policy |
|---|---|
| tool name | `[INFER]` 完整保存。 |
| args | `[INFER]` 保存 schema validation 后的 canonical JSON；对常见 secret key 名做 redaction，另存 hash。当前 repo tools一般不含 secret。 |
| result/observation | `[INFER]` 保存结构化 JSON，而非 `str(result)`；始终记录 SHA、原始 byte count、`truncated`。 |
| cap | `[INFER]` 建议 v1 每次 64 KiB。它覆盖 `read_file` 40k、单次 tests 40k、git context 12k 的常见上限；超限保留确定性 head/tail + hash。 |
| error | `[INFER]` 保存 exception type 与最多 4 KiB message；不吞掉 ToolProtocolError。 |
| source event | `[INFER]` `parent_event_id` 指向请求该 tool 的 model decision event；`subject_id` 使用 tool-call id。 |
| latency | `[INFER]` 保存整数 ms，当前已有。 |
| model-visible form | `[INFER]` 下一次 rendered user input 已完整保存，因此即使 raw tool result被 cap，仍能知道模型实际看到的 compact observation。 |

**当前 1000-char preview 不足。** `[CODE]` `telemetry.py:71-81` 使用 `str(result)[:1000]`；而 `read_file` 可返回 40k、scanner 50k、repository checks 40k（`repository_tools.py:118-128,264-343`）。它既可能截断关键行，也不是稳定 canonical JSON。

`[CODE]` 动态 `read_skill_resource` 在 `agentic_core.py:1070-1095` 直接注册 handler，不经过 `RepositoryToolSuite.registry()` 的 ledger wrapper。Trace V2 必须把 tool recording 放到 `BoundedRole`/`ToolRegistry` 的共同调用边界，或给该动态工具同样的 wrapper，不能只改 repository tools。

## 8. Run Version Snapshot

### 8.1 Capture point

`[CODE]` `service.py:144-183 ReviewService._build_agentic_reviewer()` 知道实际 active global prompt；`service.py:208-215 _active_agent_skills()` 知道 tenant 下最终 resolved disk/DB Skill；`agentic_core.py:340-392` 知道本 task 的 roles、requested skills、model client和 tool suite。

`[INFER]` 不应在 run 时重新查询“当前 DB active version”来构造历史 snapshot，因为 service 可能已持有旧 reviewer。正确做法是：build reviewer 时把实际 prompt version metadata 固化在 reviewer 对象；每次 `_review_with_context()` 从**该对象**和本次 resolved skills/config 生成 snapshot。Lead 完成 delegation 后、首个 Worker 开始前，写一次 effective snapshot；Lead 失败则写 selected_skills=[] 的 failed snapshot。

### 8.2 Minimal immutable payload

```json
{
  "schema_version": 1,
  "run_id": "<task_id>",
  "source": {
    "app_version": "0.3.0",
    "code_revision": "<git commit or archive sha>"
  },
  "prompt": {
    "name": "llm-review",
    "version": 3,
    "source": "database",
    "sha256": "..."
  },
  "skills": [
    {
      "name": "security-review",
      "version": "2",
      "source": "database",
      "content_sha256": "...",
      "selected_by": ["security-1"]
    }
  ],
  "model": {
    "provider": "...",
    "model": "...",
    "endpoint_origin": "...",
    "temperature": 0,
    "response_format": "json_object"
  },
  "runtime": {
    "mode": "agentic",
    "enabled_roles": ["lead", "security", "correctness-reliability", "critic"],
    "role_token_budget": 8000,
    "role_time_budget_seconds": 60,
    "task_max_steps": 8,
    "task_timeout_seconds": 120,
    "context_window_tokens": 32768,
    "context_input_tokens": 20000,
    "diff_tokens": 12000,
    "observation_tokens": 4000,
    "minimum_finding_confidence": 0.55,
    "max_revision_rounds": 1
  },
  "tools": {
    "by_role": {"security": ["..."]},
    "catalog_sha256": "...",
    "schema_version": 1
  },
  "created_at": "..."
}
```

### 8.3 What to include / exclude

- `[INFER]` `skills` 应包含实际 selected artifact 的 version/hash/source，以及 assignment ids。若无 Skill 被选，保存空数组；另在 snapshot 的小 `available_skill_catalog_sha256` 字段记录 Lead 看到的 catalog。
- `[INFER]` prompt 本文不必在 snapshot 重复；每个 model-call 的 rendered system prompt已保存 full content。snapshot 保存 version/hash/source 即可。
- `[INFER]` tools 只保存实际暴露给各 role 的 names 与 canonical `{name,description,parameters}` hash；不做 package manager，不保存 handler source。
- `[INFER]` `code_revision` 优先 Git commit；当前无 Git时使用 Freeze archive hash并明确类型。
- `[INFER]` repository root path、API key、GitHub token、memory正文不属于版本；repo availability和memory input属于 run context/model input。
- `[INFER]` snapshot 写后不可更新。后续 Skill selection更改只能产生新 event；resume 复用同一 snapshot id并验证 hash相等，否则 fail closed或显式记录 drift。

## 9. Persistence Design

### 9.1 Options

| Option | Advantage | Problem |
|---|---|---|
| A. 只扩展 JSON/checkpoint | `[INFER]` schema migration最少，resume天然可见 | checkpoint是按 node upsert 的恢复状态，难按 stage/subject查询；并发顺序、first divergence、Failure join都脆弱。 |
| B. 只新增 `run_events` | `[INFER]` append-only、可查询、稳定 event/finding关联 | 若完全替代 session/checkpoint会重写 resume逻辑；不符合最小改造。 |
| C. 两者结合 | `[INFER]` checkpoint继续恢复，run_events只负责归因；event ids可回写 session | 新增一张表和两套 store方法，但边界清晰。 |

**选择 C。**

### 9.2 Minimal table

```text
run_events
  id                 INTEGER/BIGSERIAL internal ordering key
  event_id           TEXT UNIQUE NOT NULL
  task_id            TEXT NOT NULL FK tasks(id)
  parent_event_id    TEXT NULL
  stage              TEXT NOT NULL
  actor              TEXT NOT NULL
  event_type         TEXT NOT NULL
  subject_id         TEXT NOT NULL DEFAULT ''
  outcome            TEXT NOT NULL
  payload_json       TEXT/JSONB NOT NULL
  error              TEXT NULL
  created_at         TEXT/TIMESTAMPTZ NOT NULL
```

- `[INFER]` 对外 `run_id = task_id`；DB 不重复存第二列。
- `[INFER]` 查询顺序用 internal `id`；event envelope 中 `sequence` 可在读取时按 id 映射，避免并行 Worker 自增冲突。
- `[INFER]` 索引只要 `(task_id,id)`、`event_id` unique、可选 `(task_id,subject_id)`。
- `[INFER]` full model/tool payload暂存 `payload_json`，避免第二张 artifacts table。每项有 SHA 和 cap；如果未来规模证明有问题再拆 blob，不在 v1 预设计。
- `[INFER]` checkpoint/session 仅增加 stable ids/version snapshot id，继续由 `_save_lead_session()` 持久化恢复状态。
- `[INFER]` `TraceEvent/trace_events` 保持原状用于 API task state，不做 backfill；旧 task 显示 `trace_v2_available=false`。
- `[INFER]` future `failure_cases.payload_json` 直接增加 `run_id/source_event_id/finding_id`，无需本轮给 failure table加列。

### 9.3 Migration impact

- `[CODE]` 项目无 migration framework；SQLite 在 `TaskStore._init()` 里 `CREATE TABLE IF NOT EXISTS`，PostgreSQL 在 `PostgresTaskStore._init()` statements 中初始化。
- `[INFER]` 新增一张独立表是最低风险 migration：不改现有 task/report/failure rows，不需要 backfill，不影响旧数据库读取。
- `[INFER]` 必须同时实现 SQLite/PostgreSQL `append_run_event()`、`list_run_events()` 与 contract test，避免再次出现 backend parity drift。
- `[INFER]` 不建议复用当前 `agent_messages`：它的 sender/recipient/message语义与 model/tool/gate lifecycle不匹配，而且当前主链也没有调用它。

## 10. Exact Code Modification Surface

以下是后续实现的建议改动面；本轮未执行。

| Priority | File | Symbol | Change needed | Reason | Complexity |
|---:|---|---|---|---|---|
| 0 | `requirements-lock.txt` (new) | N/A | clean Python 3.11 resolution后保存 exact versions/hashes | Baseline repeatability | S |
| 0 | `evaluation_data/prompt_evolution_130.jsonl` or replacement fixture | N/A | 恢复真实 artifact；不可恢复时重建明确 proof fixture/contract | 解除 evolution proof blocker | M / `[UNKNOWN]` data provenance |
| 0 | `evoagent/evaluation_experiments.py` | `prepare_controlled_experiment_cases()`、`AccuracyExperimentSuite.run_controlled()` | 将 current synthetic development set 的合法、非生产 provenance 与 tests统一；不放宽 production readiness | Baseline command/test一致 | S |
| 0 | `tests/test_evaluation_harness.py`、`tests/test_evaluation_experiments.py` | dataset contract tests | 与真实 `synthetic-controlled` 定位一致，并继续断言 production claim=false | 避免通过伪造 metadata换 green | S |
| 1 | `evoagent/store.py` | `TaskStore._init()` + new append/list methods | 新 `run_events` table、indexes、JSON encode/decode | SQLite Trace V2 persistence | M |
| 1 | `evoagent/postgres_store.py` | `_init()` + matching methods | 与 SQLite 完全同 contract | Backend parity | M |
| 1 | `evoagent/telemetry.py` | `ExecutionLedger`、`ModelCall`、`ToolCall` | 加 run id/event sink、stable event IDs/parents、structured payload、restore兼容 | 复用现有 telemetry作为统一 producer | M |
| 2 | `evoagent/llm.py` | `JsonChatClient.complete_json()` | 在 JSON parse前记录 exact request/raw content；记录 parsed response、request params、response metadata allowlist | 定位 model vs parser divergence | M |
| 2 | `evoagent/service.py` | `_build_agentic_reviewer()`、`_active_agent_skills()`、`_create_task()` | 将实际 prompt version metadata传入 reviewer；提供 source revision/runtime settings | Snapshot必须反映对象实际使用值，不是事后查当前 DB | M |
| 2 | `evoagent/agentic_core.py` | `_review_with_context()`、`_agentic()`、`_run_lead()`、`_run_pending_assignments()` | 创建 effective RunVersionSnapshot；给 assignment/model/tool调用建立 parent ids | 主 attribution spine | L |
| 3 | `evoagent/agentic_core.py` | `_parse_findings()`、`_session_candidates()`、`_merge()` | candidate/finding ids、raw parse rejection、merge lineage | 找 candidate first divergence | L |
| 3 | `evoagent/agentic_core.py` | `_run_critic()`、`_apply_critic()`、`_apply_lead_final()` | prompt/decision中携带 finding_id；index保留兼容；记录 explicit selected/not-selected | 连接 Critic→Lead→published | M |
| 3 | `evoagent/gates.py` | `FindingGate.apply()` / `GateResult` | 每 finding 输出 stable id + enum reason codes；保留现有文字 | 可靠判定 terminal gate divergence | S |
| 3 | `evoagent/models.py` | `Finding`；new `RunVersionSnapshot` dataclass/TypedDict | optional finding_id/candidate_ids；小型 snapshot schema | 降低自由 dict drift | M |
| 4 | `evoagent/repository_tools.py` | `RepositoryToolSuite.registry()` | structured result、64 KiB policy、parent event；统一 args/error | 1000-char preview不足 | M |
| 4 | `evoagent/agentic_core.py` | `_register_skill_resource_tool()` | 让动态 resource tool进入同一 recording path | 修复当前 tool trace盲点 | S |
| 4 | `evoagent/harness.py` | `ReviewHarness.run()` / `_reviewing()` | 接 AgentRuntime event sink；run/report/published/failure terminal events | 补 run首尾和 runtime retry | M |
| 5 | `tests/test_trace_v2.py` (new) | lifecycle/store/model/tool/snapshot tests | 断言 event因果链、resume不重复、IDs稳定、secret不落库、tool截断、first divergence | Trace V2 acceptance contract | M |
| 5 | existing critical tests | service/lead-worker/runtime/evaluation/storage tests | 增加 finding IDs 后保持原输出兼容；补 SQLite/Postgres method parity | 防止 tracing改变行为 | M |

`[INFER]` 最大单点是 `agentic_core.py`，但不需要整体重构。以 ExecutionLedger sink + sidecar lifecycle IDs 为边界，控制为几组局部修改即可。

## 11. Complexity / Risks

### Complexity

- Baseline environment/data reconciliation：**M**。依赖 lock很小，难点是缺失 130-case artifact和当前 provenance冲突。
- Trace event/store plumbing：**M**。一张表、两 backend、一个统一 sink。
- Exact model/tool snapshots：**M**。调用点集中，但需处理 parse failure、redaction、payload cap。
- Candidate/finding lifecycle：**L**。当前依赖 indices/List[Finding]，merge会丢 lineage，是最容易引入行为回归的部分。
- RunVersionSnapshot：**M**。字段不多，难点是捕获“实际 reviewer object 使用的版本”以及 Lead 后确定的 selected Skill。

### Main risks

1. `[CODE]` Worker并发：role-local ledger sequence不能代表全局顺序；必须依赖 append id + parent_event_id。
2. `[CODE]` resume：checkpoint可能恢复旧 ledger；stable candidate/finding ids必须确定性，append要避免重复 terminal events。
3. `[INFER]` storage growth：完整 5+ model inputs可能每 task数百 KB；本地三个月项目可接受，但需 cap tool result和明确 retention。
4. `[INFER]` privacy：user input、memory和repo代码会进入 model snapshot；需要 secret redaction与可见的 content retention状态。
5. `[CODE]` Critic不是过滤器：归因逻辑若误把 critic rejection当 terminal，会产生错误标签。
6. `[CODE]` final FindingGate发生在 Lead之后；事件顺序设计不能按 README/常见架构重排事实。
7. `[INFER]` LLM baseline不能保证 bitwise reproducibility；Freeze定义应是 environment/protocol/artifact reproducibility，并如实记录输出波动。
8. `[CODE]` SQLite/PostgreSQL已有能力偏差历史；Trace V2必须先定义共同 store contract。

## 12. Explicit Non-Goals

Failure Attribution v1 **不需要**：

- hidden chain-of-thought、private reasoning、token-by-token generation 或 logprobs；
- 完整 distributed tracing platform、Jaeger/Tempo部署或生产 OTel backend；
- full repository tarball/snapshot；已有 diff hash、repo identity和model-visible context足够，必要证据由 tool snapshots覆盖；
- 任意 tool/interface evolution、自动写工具代码或 schema mutation；
- 完整 canary/shadow 修复；它与先建立可信单-run attribution无关；
- PostgreSQL/Redis/GitHub live certification 作为 Baseline Freeze硬条件；SQLite/in-memory/fake GitHub已足以冻结算法 baseline；
- 新的 agent framework、消息总线、event sourcing重写或新的 RAG/MCP；
- 保存所有中间 Python对象或每个 function enter/exit；
- 复杂前端 Trace viewer；最初用 store query/JSON report验证即可；
- 全代码库 source hash作为每次 model call payload；一个 run-level code revision足够；
- 一开始就自动判断唯一 root cause。Trace V2 只提供可审计事实，允许 attribution=`unknown`。

## 13. Recommended Implementation Order

1. **Freeze inputs before tracing changes。** `[INFER]` 建 Python 3.11 exact lock；统一 `synthetic-controlled` development provenance；恢复/替代缺失 130-case fixture；跑到 70/70 green。
2. **保存 baseline artifacts。** `[INFER]` 先跑 deterministic，随后固定一个 LLM config跑 full-agentic development baseline，再跑 prompt/Skill evolution baseline；写 manifest/hash，不改算法。
3. **先加 append-only store contract。** `[INFER]` SQLite/PostgreSQL 同时新增 `run_events` 和 tests；保留旧 TraceEvent/checkpoint。
4. **把 ExecutionLedger 接成统一 sink。** `[INFER]` 先记录 runtime/model/tool start-complete/error和因果 parent；此时不动 finding逻辑。
5. **加入 model/tool exact snapshots。** `[INFER]` raw response必须在 JSON parse前落事件；tool用structured JSON + 64 KiB policy；验证没有 secret。
6. **加入 immutable RunVersionSnapshot。** `[INFER]` 固化实际 reviewer的 prompt metadata、selected Skill hashes、model/config/tool catalog；写 resume drift test。
7. **最后加入 candidate/finding lifecycle IDs。** `[INFER]` 按 generated→parse→merge→critic→lead→gate→published逐段加，保持 existing indices作兼容字段。
8. **重跑 frozen baseline。** `[INFER]` deterministic metrics必须精确不变；fake-client topology/call counts必须不变；LLM结果允许自然波动但执行协议、trace completeness和version snapshot必须通过。
9. **Trace V2 green 后再开始 Failure Attribution v1。** `[INFER]` 不在本阶段实现 taxonomy classifier/evolution routing。

# CHATGPT_HANDOFF_V2

仓库：`/Users/bytedance/Downloads/EvoAgent(3)`；`BASELINE_FREEZE=NOT_READY`。

`[RUNTIME]` 当前仅有Python 3.9.6，无python3.11，第三方依赖未安装。`[CODE] requirements.txt:1-6` 的版本范围不是可冻结lock；3.11兼容性仍需clean实测。测试共70个methods；当前21 pass、9 error entries：8个模块缺PyYAML，另一个缺 `prompt_evolution_130.jsonl`。`pr_diff_100.jsonl` 的source是 `synthetic-controlled`，却有测试要求 `offline-fixture`；不可伪造metadata。它只能是development regression set。

Baseline分三类：① deterministic入口 `AccuracyExperimentSuite.run_controlled()`，CLI `scripts/run_accuracy_experiment.py`，正式结果是Local+Context composite；② multi-agent LLM入口 `scripts/run_agentic_evaluation.py` 的full-agentic arm，经 `ProductArmReviewer` 调真实reviewer，需固定model/预算/seed/API；当前100例只用 `--allow-non-production-data`；③ self-evolution需冻结prompt proof（缺130例集）和Skill experiment（缺train split/API）。Freeze门槛：源码hash、Python 3.11 patch、hash lock、数据provenance、70/70 green、三类结果JSON和无secret manifest。

`[CODE]` review顺序：runtime→context/scanner→Lead delegation→Worker/model/tool→parse→merge→Critic→Lead final→FindingGate→report。`runtime.py:140-218`虽有RuntimeEvent，Harness未传event_sink；`telemetry.py`只记用量和1000字符tool preview；`llm.py:25-79`不存实际prompt/raw/parsed response；parse静默丢无效定位，merge丢lineage，Critic/Lead只用index，且Critic拒绝不直接过滤candidate。

Trace V2最小字段：`event_id, run_id, parent_event_id, sequence, stage, actor, event_type, subject_id, outcome, reason_codes, refs, data, error, created_at`。保留TraceEvent和checkpoint/session，扩展ExecutionLedger为统一producer；SQLite/PostgreSQL新增append-only `run_events`。

使用两级稳定ID：`candidate_id`表示单次产物；`finding_id`由run+location+canonical identity生成。merge记录contributors/winner；生命周期为generated→parsed/rejected→merged→critic decision→lead selected/not selected→gate accepted/rejected→published，以定位first divergence。Critic decision不一定终止。

Model snapshot保存实际system prompt、managed user JSON、API可见raw response、parsed JSON及SHA，加provider/model/params/usage/latency/error；禁止key、headers和hidden chain-of-thought。Tool保存validated args与structured result；1000字符不足，v1用64KiB上限并记full hash/size/truncated/source event。`read_skill_resource`也要覆盖。

Immutable `RunVersionSnapshot` 从实际reviewer构造：source revision、prompt version/hash、实际Skill hash/assignment、provider/model、roles/预算/context/gate配置、各role tool catalog hash。来源在 `service.py:144-215` 与 `agentic_core.py:340-392`；恢复时复用并检查drift。

顺序：先冻结环境、数据、70 tests和三类baseline，再做store→ledger→model/tool snapshot→version snapshot→candidate lifecycle，最后实现Failure Attribution。v1不做hidden reasoning、分布式追踪、全仓快照、tool evolution、shadow/canary、复杂前端或runtime重写。
