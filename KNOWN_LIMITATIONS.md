# Known Limitations

这里只记录 **C — Design Limitation**。本阶段不修改这些项目。

1. **无模型配置时不能从 Review API 使用 deterministic fallback。** `ReviewService.create_review()` 在 `service.py:281-284` 强制 `_require_agentic_model()`；服务可启动、可看 health/UI，但 review 必须有模型。当前行为一致且有测试，不是 runtime bug。

2. **Prompt evolution 与 Agent Skill evolution 是两套并行机制。** 前者存 `skill_versions.prompt` 并由 `EvolutionEngine` 管理；后者存 versioned `SKILL.md` artifact 并由 `SkillEvolutionEngine` 管理。两者都能工作，但概念和 activation path 不统一；本阶段不合并。

3. **Active version 由数据库指针决定，但长生命周期 service 的 prompt reviewer 需要重建才能看到变化。** `_build_agentic_reviewer()` 在构造时读取 active prompt；`reload_skills()` 会重建 reviewer。数据库 activation 本身不会修改已经存在的 reviewer object。本阶段不改生命周期。

4. **现有 execution trace 是聚合型而非完整决策轨迹。** checkpoint、lead session、agent traces 和 ledger 足以排查常见运行问题，但不保存每次实际 prompt/raw model response。本阶段明确暂停 Trace V2。

5. **OpenTelemetry provider 是进程全局状态。** 同一测试进程反复创建 `ReviewService` 会打印 `Overriding of current TracerProvider is not allowed`，但测试仍通过，服务功能未失败。

6. **Critic decision 与最终过滤解耦。** `_apply_critic()` 记录 accept/objection 并调整置信度，但返回全部 candidates；Lead final 才决定选择，之后还有 FindingGate。行为可运行但容易被误读，本阶段只记录。

7. **`agentic_core.py` 集中承担编排、角色 prompt、Skill 注入、merge、Critic 和 session 恢复。** 文件较大但当前 tests 覆盖主要协作路径；本阶段不因可维护性偏好拆分。

8. **canary/shadow 的配置和状态存在，但没有在本轮证明完整线上流量闭环。** 单元测试覆盖 assignment/error-budget 行为；未接真实模型、队列和生产流量。本阶段不扩展。

