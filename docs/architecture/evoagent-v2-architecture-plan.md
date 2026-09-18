# EvoAgent v2 宏观改造设计书 v0.1

> 文档版本：v0.1  
> 最后更新：2026-09-17

## 变更记录

| 版本 | 日期 | 变更说明 |
|---|---|---|
| v0.1 | 2026-09-17 | 建立 EvoAgent v2 宏观架构与分阶段改造路线。 |

## 1. 项目最终想变成什么

EvoAgent 当前已经是一个能够完成 PR Review 的 Multi-Agent 系统。

下一阶段的目标不是继续堆更多 Agent，而是把它改造成一个：

> **能够观察自身执行过程、解释失败发生在哪里、针对正确能力层进行有限自我改进，并安全发布/回滚新版本的 Agent 系统。**

最终主线：

```text
PR Review
↓
Observable Execution
↓
Failure Signal
↓
Failure Attribution
↓
Targeted Evolution
↓
Independent Evaluation
↓
Versioned Promotion
↓
Rollback
```

一句话描述：

> EvoAgent 不再是“失败后改 Prompt”，而是“先诊断，再定向改进，再验证，再发布”。

---

# 2. 整体架构思路

未来系统可以宏观理解成五层。

```text
┌──────────────────────────────┐
│ Layer 1  Review Runtime      │
│                              │
│ Scanner / Worker / Critic    │
│ Lead / Gate                  │
└──────────────┬───────────────┘
               ↓
┌──────────────────────────────┐
│ Layer 2  Observable Execution│
│                              │
│ Candidate lifecycle          │
│ Evidence / provenance        │
│ Decisions / versions         │
└──────────────┬───────────────┘
               ↓
┌──────────────────────────────┐
│ Layer 3  Failure Attribution │
│                              │
│ First Divergence             │
│ Root Cause                   │
│ Evolution Surface Routing    │
└──────────────┬───────────────┘
               ↓
┌──────────────────────────────┐
│ Layer 4  Evolution Pipeline  │
│                              │
│ Prompt / Skill patch         │
│ Candidate evaluation         │
│ Selection                    │
└──────────────┬───────────────┘
               ↓
┌──────────────────────────────┐
│ Layer 5  Release Governance  │
│                              │
│ Release Bundle               │
│ Task Pinning                 │
│ Promotion / Rollback         │
└──────────────────────────────┘
```

Evaluation 不是单独最后做，而是横穿整个系统：

```text
               Evaluation Governance
                        │
       ┌────────────────┼────────────────┐
       ↓                ↓                ↓
 Attribution        Evolution        Final System
 Evaluation         Evaluation       Benchmark
```

---

# 3. 五个 Idea 在整体设计中的位置

## IDEA-002

Evidence-Preserving Bounded Review Loop

定位：

```text
Review Architecture Enhancement
```

它主要优化：

* Candidate provenance；
* Critic verification；
* evidence challenge；
* Gate repair；
* Review decision quality。

不作为整个项目的第一主线，但保留为高价值增强方向。

---

## IDEA-003

Persistent Artifact Store + Private Working-Set Context

定位：

```text
Agent Context / Evidence Infrastructure
```

它主要解决：

* 原始 Evidence 怎么保存；
* Context Window 怎么只保留工作集；
* Agent 怎么重新读取证据；
* Agent Context 如何相互隔离。

同样保留。

但不会一开始就建设完整 Artifact / Memory Platform。

---

## IDEA-004

Evidence-Backed Attribution + Targeted Evolution

定位：

```text
项目第一核心创新
```

负责回答：

```text
系统哪里失败了？
↓
为什么失败？
↓
应该修改哪里？
```

它是整个 Self-Evolution 主线的“大脑”。

---

## IDEA-005

Evaluation Governance + Focused Benchmark

定位：

```text
实验与可信度基础设施
```

负责回答：

```text
改完真的更好吗？

以及：

为什么更好？
```

不需要做成大型学术评测平台。

---

## IDEA-006

Unified Evolution Pipeline + Versioned Promotion

定位：

```text
Evolution Engineering Backbone
```

负责回答：

```text
已经知道该改哪里之后，
如何生成修改，
如何验证，
如何发布，
如何回滚？
```

---

# 4. 整个项目分成六个阶段

---

## Phase 0 — Freeze Baseline & Evaluation Contract

### 目标

先定义：

> “以后我们说系统变好了，到底是什么意思？”

这一阶段不改 Agent 架构。

确定：

```text
Development Regression Set
Final Benchmark 的定位
核心 metrics
实验公平性规则
```

当前 synthetic-controlled 数据继续作为：

```text
Development Regression Set
```

而不是最终宣传数据。

### 这一阶段主要属于

```text
IDEA-005
```

### 产出

至少冻结：

```text
Core Review Metrics

Precision
Recall
F1
High-risk Recall
Clean Accuracy
Cost / Tokens / Latency
```

Self-Evolution 再增加：

```text
Attribution Accuracy
Wrong-Surface Rate
Evolution Success Rate
Regression Rate
```

但不是现在就把所有 benchmark 做完。

---

# Phase 1 — Observable Execution

这是第一个真正的代码改造阶段。

### 核心问题

现在系统能够告诉我们：

> 最终输出了什么。

但未来 Attribution 需要知道：

> 一个 Finding 在整个流程中经历了什么。

例如：

```text
Security Worker
产生 Candidate C17
↓
merge
↓
Critic
↓
Lead
↓
Gate
↓
Publish / Drop
```

当前 live code 调查已经确认：

* 没有贯穿整个生命周期的 stable candidate/finding ID；
* merge、Critic、Lead、Gate 使用不同方式关联 Finding；
* Critic / Lead 很大程度依赖临时 list index；
* 当前无法对任意 Candidate 完成完整 deterministic first-divergence attribution。

所以这一阶段不是“做 Self-Evolution”。

而是：

> **给系统安装黑匣子。**

### 希望未来能够回答

```text
Candidate 谁产生的？

它在哪次 merge 中发生了什么？

Critic 怎么判断？

Lead 怎么判断？

Gate 为什么接受 / 拒绝？

最终什么时候消失？
```

### 原则

必须尽量：

```text
Behavior Preserving
```

即：

```text
不改 Worker 逻辑
不改 Critic 逻辑
不改 Lead policy
不改 Gate 条件
```

只提高 observability。

当前调查也表明，可以优先利用已有 checkpoint/session，而不必首先建设全新的 Artifact Platform。

### 这一阶段吸收

```text
IDEA-002 的 provenance 思想
+
IDEA-003 的 evidence 思想
```

但不完整实现 002 / 003。

---

# Phase 2 — Failure Attribution

这是项目最关键的阶段之一。

系统开始真正回答：

> “为什么这次 Review 失败？”

结构上分两步。

---

## Step A — First Divergence

先回答：

> **第一次在哪里偏离正确路径？**

例如：

```text
应该产生 Candidate
但 Worker 没产生
→ WORKER_STAGE
```

或者：

```text
Worker 已产生
merge 后消失
→ MERGE_STAGE
```

或者：

```text
一直存在
Lead Final 没选
→ LEAD_STAGE
```

这是尽量 deterministic 的。

原则：

> 能由代码确定的，不让 LLM 猜。

---

## Step B — Root Cause

知道“哪里第一次出错”以后，再问：

> 为什么？

例如：

```text
WORKER_STAGE
```

仍然可能因为：

```text
Context 中根本没有关键事实

Skill 缺必要知识

Tool 没有获取证据

Prompt 发生冲突

模型看到证据但推理失败
```

这些才可能需要 Attribution Agent。

输出允许：

```text
SUPPORTED

INSUFFICIENT_EVIDENCE

UNKNOWN
```

不强迫所有 failure 都有唯一答案。

---

## Phase 2 最终输出

类似：

```text
Failure
FALSE_NEGATIVE

First Divergence
SECURITY_WORKER

Root Cause
Security Skill lacks required authorization reasoning guidance

Evolution Surface
SKILL(security-review)

Status
SUPPORTED
```

也可能：

```text
Evolution Surface
NO_SUPPORTED_EVOLUTION

Status
UNKNOWN
```

后者是正常结果。

---

# Phase 3 — Targeted Evolution

Phase 2 回答：

```text
应该改哪里？
```

Phase 3 才真正开始：

```text
怎么改？
```

MVP 只允许两个 Evolution Surface：

```text
GLOBAL_PROMPT

SKILL
```

暂时不允许：

```text
Tool code self-evolution
Context policy self-evolution
model self-selection
runtime code rewriting
architecture self-modification
```

---

## Global Prompt

只处理：

```text
真正系统级、
跨角色共同存在的问题。
```

---

## Skill

处理：

```text
某个特定领域或 Worker 的能力缺口。
```

例如：

```text
authorization reasoning gap
→ Security Skill
```

```text
transaction reasoning gap
→ Reliability Skill
```

---

## 第一版不要复杂搜索

先：

```text
Attribution
↓
1 targeted candidate
↓
Evaluation
```

以后再尝试：

```text
1 candidate
vs
2 candidates
```

Bounded Candidate Search 仍然保留在 IDEA-006 中，但不作为主链成立的前提。

---

# Phase 4 — Versioned Evolution & Release Governance

这一阶段解决：

> Self-Evolution 改完之后，怎么防止系统越来越不可控？

未来不只是：

```text
Prompt v3 active=true
```

而是：

```text
Release R17
```

R17 表示整个 Agent Runtime 行为版本。

例如概念上：

```text
Release R17

Prompt
v3

Security Skill
v2

Reliability Skill
v4

Model
固定版本 / 配置

Runtime
固定 budget

Context
固定 policy

Tools
固定 catalog

Gate
固定 config
```

---

## Per-Task Pinning

Task 开始时：

```text
resolve current release
↓
pin R17
```

之后：

```text
整个 Task 一直使用 R17
```

即使运行过程中：

```text
Promote R18
```

老 Task 也不能半路漂移。

---

## Rollback

例如：

```text
Active = R20
↓
发现问题
↓
Rollback R19
```

语义明确：

```text
新的 Task
→ R19

已经运行中的 R20 Task
→ 按预定义策略继续 / cancel
```

这一阶段主要证明的是：

```text
correctness
reproducibility
rollback safety
```

不需要专门证明：

```text
F1 + X%
```

Integration Test 就足够有价值。

---

# Phase 5 — 第二个 Agent Engineering 亮点

到这里：

```text
Observable Execution
→ Attribution
→ Targeted Evolution
→ Versioned Promotion
```

主线已经完整。

这时候再决定第二个重点 Enhancement。

候选：

```text
IDEA-002
vs
IDEA-003
```

---

## Option A — IDEA-002

如果我们发现当前 Review verification 是明显瓶颈：

```text
Critic 经常知道证据不足

但没有能力请求补证
```

那么增加：

```text
Independent Worker Analysis
↓
Anonymous Critic
↓
One bounded evidence challenge
↓
Final decision
```

这是：

> Multi-Agent Verification Architecture

可以成为第二个有少量 A/B 指标的亮点。

---

## Option B — IDEA-003

如果我们发现 Attribution / Review 经常因为：

```text
Tool Result 截断
Context 丢失
跨文件 evidence retention 差
```

而受到限制，那么继续发展：

```text
Persistent Artifact
+
Private Agent Working Set
+
Explicit Evidence Reread
```

这是：

> Context Engineering / Agent Memory Architecture

同样可以成为第二个亮点。

---

## 重要原则

不是现在就决定：

```text
002 和 003 只能活一个
```

而是：

> **哪个在真实改造过程中暴露出最高价值，就先深化哪个。**

另一个仍然保留在 Idea Pool。

---

# Phase 6 — Final Validation & Project Packaging

最后阶段不是继续堆功能。

而是收尾。

---

## Flagship Experiment

最重要的一组实验：

```text
A
Baseline EvoAgent

B
Naive Self-Evolution
Failure
→ Global Prompt Patch

C
Targeted Self-Evolution
Failure
→ Attribution
→ Correct Surface
→ Targeted Patch
```

回答：

```text
Attribution 是否正确？

Wrong-surface update 是否降低？

Regression 是否降低？

Evolution success 是否提高？

最终 Review quality 是否改善？

成本增加多少？
```

---

## 第二个实验

只在 002 / 003 中选择真正做成核心亮点的那个。

例如：

```text
One-shot Critic
vs
Bounded Evidence Challenge
```

或者：

```text
Current Context
vs
Artifact-backed Private Working Set
```

只需要一组有意义的小实验。

---

# 5. 三个月大致节奏

不是死计划，只是顺序。

```text
Weeks 1–2
Phase 0
Evaluation Contract
+
Phase 1
Observable Execution

Weeks 3–4
Failure Attribution
first-divergence

Weeks 5–6
Evidence-backed Attribution
+
Surface Routing

Weeks 7–8
Targeted Prompt / Skill Evolution

Weeks 9–10
Release Bundle
Task Pinning
Promotion / Rollback

Weeks 10–11
深化 IDEA-002 或 IDEA-003

Weeks 11–12
Final Benchmark
Flagship Experiments
Demo / README / Resume Packaging
```

如果中途某一阶段远比预期复杂：

> 后面的 Feature 可以缩，但核心闭环不能断。

---

# 6. 项目优先级

遇到 scope 冲突时，按照：

```text
P0
完整跑通：
Failure
→ Attribution
→ Targeted Evolution
→ Evaluation
→ Versioned Promotion / Rollback

P1
第二个 Agent Engineering 亮点
002 或 003

P2
Candidate Search / richer Artifact / richer Review Loop

P3
Canary / Shadow
复杂 automation
外围平台能力
```

---

# 7. 不要求所有改进都有 Benchmark

以后每个 Feature 先判断它属于哪一种 Claim。

## 效果 Claim

例如：

```text
Targeted Evolution 比 naive Prompt Evolution 更好
```

必须 Benchmark。

---

## Architecture Claim

例如：

```text
Task 不发生 mid-run version drift
```

Integration Test 即可。

---

## Correctness Claim

例如：

```text
Rollback 后新 Task 使用旧 Release
```

测试即可。

---

## Engineering Feature

例如：

```text
Candidate provenance 能完整查询
```

contract / runtime evidence 即可。

---

所以最终大约只有：

```text
1 个主旗舰实验

+

最多 1 个第二亮点实验
```

需要认真做指标。

---

# 8. 当前版本明确不做

三个月内不主动扩展：

```text
通用 A2A protocol

通用 Agent orchestration framework

完整 Memory / RAG Platform

Tool code self-evolution

Context policy autonomous evolution

Model/provider self-evolution

architecture self-rewriting

大型 Canary / Shadow platform

复杂前端

企业级 Redis/Postgres 重构

大量无意义 ablation
```

---

# 9. 每个阶段的统一开发方式

虽然这是宏观设计书，但后续进入每个阶段都遵循同一个闭环：

```text
宏观目标
↓
重新调查对应 live code
↓
冻结局部事实
↓
设计最小修改
↓
实现
↓
Unit / Integration / Runtime Evidence
↓
判断是否继续下一阶段
```

Architecture Design 不替代 live-code investigation。

这份设计书只是：

```text
路线图
```

不是：

```text
代码事实
```

---

# 10. 最终项目应该呈现出的形态

最终 EvoAgent 不只是：

```text
Multi-Agent PR Reviewer
```

而是：

```text
Observable
Multi-Agent PR Reviewer

+

Evidence-Backed
Failure Attribution

+

Targeted
Self-Evolution

+

Independent
Evaluation

+

Versioned
Promotion / Rollback
```

整个项目最终最有辨识度的一句话是：

> **Built an evidence-backed self-evolving multi-agent PR reviewer that diagnoses where failures first occur, routes improvements to targeted Prompt/Skill surfaces, independently evaluates candidates, and deploys changes through reproducible versioned releases with rollback.**

而 IDEA-002 / IDEA-003 则作为进一步体现 Multi-Agent Verification 或 Context Engineering 深度的第二条技术亮点。
