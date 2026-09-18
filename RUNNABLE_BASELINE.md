# RUNNABLE_BASELINE

日期：2026-09-16  
状态：**ACHIEVED_WITH_KNOWN_TEST_GAPS**

该状态表示本地 Python 环境、服务、fake-model Agent Review 主链和 deterministic development benchmark 均已实际运行；它不表示缺失评测 artifact 已恢复，也不表示当前 development dataset 满足 production benchmark contract。

## Python 3.11 环境

- 环境：`/Users/bytedance/Downloads/EvoAgent(3)/.venv`
- Python：CPython 3.11.16
- pip：26.2.1
- 创建工具：uv 0.12.14，工具位于 `/tmp/evoagent-uv-0.12.14`；未修改系统 Python。
- 安装来源：原始 `requirements.txt`，另安装环境管理工具 `pip`。
- 实际包：certifi 2026.7.22、cffi 2.1.1、charset-normalizer 3.5.1、cryptography 50.0.1、googleapis-common-protos 1.75.3、idna 3.19、opentelemetry-api/sdk/exporter 1.44.0、opentelemetry-proto 1.44.0、opentelemetry-semantic-conventions 0.65b0、pip 26.2.1、protobuf 7.36.1、psycopg/psycopg-binary 3.3.5、pycparser 3.0、PyJWT 2.14.0、PyYAML 6.0.3、redis 6.4.0、requests 2.34.2、typing-extensions 4.16.0、urllib3 2.8.0。

激活方式：

```bash
source .venv/bin/activate
```

## Tests

命令：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest discover -s tests -v
```

结果：70 tests；**66 passed、2 failures、2 errors**。

| 分类 | 测试 | 状态与证据 |
|---|---|---|
| Confirmed test bug，已修 | `test_safe_fixer_changes_only_supported_rules` | Python 3.11 `ast.unparse()` 合法输出单引号；测试原来硬编码双引号。仅将断言改为接受两种引号，单测与完整 discovery 均通过。 |
| Test/code/data contract mismatch | `test_generated_dataset_has_repository_level_split_and_expected_counts` | 测试要求 `offline-fixture`，当前文件全部是 `synthetic-controlled`。 |
| Test/code/data contract mismatch | `test_controlled_adapter_creates_repository_disjoint_60_20_20_splits` | adapter 在 `evaluation_experiments.py:49-77` 拒绝非 `offline-fixture`。 |
| Test/code/data contract mismatch | `test_controlled_accuracy_reports_full_metric_contract` | benchmark 正确把 `offline_fixture_provenance` 标为 false，因此 test 的 `assertTrue` 失败。 |
| Missing artifact | `test_feedback_evolution_improves_repository_disjoint_holdout` | 缺少 `evaluation_data/prompt_evolution_130.jsonl`。 |

### `pr_diff_100.jsonl` 判断

- 文件内容、`source.generator=evoagent-e2e-v1` 与 `source.kind=synthetic-controlled` 相互一致；它确实是合成受控数据。
- 文件 mtime 晚于相关 adapter/tests；在无 Git 历史时，这只支持“测试/adapter 更可能仍是旧 contract”，不能证明作者意图。
- 若当前文件是权威输入，语义上应更新测试和受控 adapter，使其显式接受 `synthetic-controlled`，同时继续关闭 production/generalization claim；不应把文件改名为 `offline-fixture`。
- 该改动涉及 benchmark contract，不在证据不足时实施。当前唯一诚实定位是 **development regression dataset**。

### `prompt_evolution_130.jsonl` 判断

- 直接依赖：`tests/test_evolution_proof.py`；CLI `scripts/run_prompt_evolution_proof.py` 也默认依赖它。
- `evolution_proof.py:39-41` 的 `generate_prompt_evolution_cases()` 只是兼容名称，实际调用 loader。
- 仓库只有 JSONL writer，没有 130-case generator，也没有其他副本。
- 当前更符合“预生成 artifact 漏提交或当前仓库副本不完整”，不能从现有代码忠实重建。
- 它可被视为独立 proof test 的 quarantine 候选，但本轮不添加 silent skip；在 artifact 来源明确前保留显式失败更诚实。

## Service

验证命令：

```bash
EVOAGENT_HOST=127.0.0.1 \
EVOAGENT_PORT=18080 \
EVOAGENT_DB_PATH=/tmp/evoagent-maintenance-20260916.db \
EVOAGENT_AUTH_REQUIRED=false \
EVOAGENT_LLM_PROVIDER=local \
EVOAGENT_DATABASE_URL= \
EVOAGENT_REDIS_URL= \
EVOAGENT_GITHUB_TOKEN= \
EVOAGENT_OTEL_ENDPOINT= \
.venv/bin/python -m evoagent
```

实测：

- `GET /health`：200，SQLite、memory-acked queue、无模型。
- `GET /`：200，返回管理台 HTML。
- `GET /api/dashboard`：200。
- `GET /api/skills`：200，返回 builtin scanners 和磁盘 Agent Skills。
- 无模型时 `POST /v1/reviews`：500，明确错误 `agentic review requires a configured model`；这是现有显式契约，不是意外 crash。

## Review 主链

使用 `tests/test_phases_0_5.py::FakeChatClient`、临时 SQLite 和临时 HTTP server 实际执行 `POST /v1/reviews`：HTTP 201、API/task 均为 `SUCCESS`、6 次模型调用、四个角色均执行、发布 `SEC-EVAL`。详见 `REVIEW_FLOW_NOTES.md`。

## Deterministic development benchmark

命令：

```bash
.venv/bin/python scripts/run_accuracy_experiment.py \
  --output output/accuracy-experiment/development-regression-baseline-20260916.json
```

- Dataset：`evaluation_data/pr_diff_100.jsonl`
- SHA-256：`e3cf61546e0f554c045a79949202532046f7e936d8bbb1e2f5d0de2b79634d74`
- Reviewer：`local-rules+context-security-reliability-agent`
- Precision：0.825
- Recall：0.825
- F1：0.825
- High-risk recall：0.9474
- Clean accuracy：0.9167
- Contract：`dataset_contract_passed=false`，唯一失败项为 `offline_fixture_provenance=false`。
- Result SHA-256：`3e745624250740d797caf89e51c3a1d87d515485d593a4078b8991b4ac638c33`

这些数字只描述该 development regression dataset，不支持 production 或 generalization claim。
