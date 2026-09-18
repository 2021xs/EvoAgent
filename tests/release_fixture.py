"""Release-pinning fixture for tests that invoke AgenticReviewer directly."""

from evoagent.agentic_core import AgenticReviewer as RuntimeAgenticReviewer


class PinnedAgenticReviewer(RuntimeAgenticReviewer):
    """Give legacy direct-review fixtures the production task-creation invariant."""

    def review_with_context(
        self, task_id, diff, parsed, repository="", tenant_id="default",
    ):
        task = self.store.get(task_id, tenant_id)
        if task and not task.get("release_id"):
            task_input = task.get("input") or {}
            roles = set(task_input.get("enabled_agents") or self.enabled_roles)
            requested = [str(value) for value in task_input.get("enabled_skills") or []]
            scanners = self.scanners + (
                list(self.scanner_provider(tenant_id)) if self.scanner_provider else []
            )
            skills = {
                skill.name: skill for skill in (
                    list(self.skill_provider(tenant_id)) if self.skill_provider else []
                )
            }
            release = self.store.put_release(
                tenant_id, self.build_release_spec(skills, roles, requested, scanners),
            )
            with self.store._connect() as conn:
                conn.execute(
                    "UPDATE tasks SET release_id=? WHERE id=? AND release_id=''",
                    (release["release_id"], task_id),
                )
        return super().review_with_context(
            task_id, diff, parsed, repository, tenant_id,
        )
