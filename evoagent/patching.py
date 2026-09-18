"""LLM unified-patch generation with structural and sandbox verification."""
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Callable, Dict, List, Optional

from .llm import JsonChatClient
from .telemetry import ExecutionLedger
from .verifier import RepairVerifier


PATCH_PROMPT = """You are the Fix Agent. Produce one minimal unified diff that fixes the supplied
verified findings without unrelated changes. Patch paths must be among allowed_paths. Preserve
behavior except for the defect. Return JSON only: {"patch":"--- a/path\n+++ b/path\n@@ ...",
"behavioral_claims":["..."],"related_tests":["..."]}. Do not use Markdown fences. If evidence is
insufficient, return {"patch":"","reason":"..."}."""


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: List[str]


@dataclass
class FilePatch:
    path: str
    hunks: List[Hunk]


HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_unified_patch(value: str, allowed_paths: List[str]) -> List[FilePatch]:
    lines = value.replace("\r\n", "\n").splitlines()
    patches: List[FilePatch] = []
    index = 0
    allowed = {item.replace("\\", "/") for item in allowed_paths}
    while index < len(lines):
        if not lines[index].startswith("--- "):
            if lines[index].strip():
                raise ValueError("patch contains content outside a file header")
            index += 1
            continue
        old_path = lines[index][4:].split("\t", 1)[0]
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise ValueError("patch is missing new-file header")
        new_path = lines[index][4:].split("\t", 1)[0]
        index += 1
        path = new_path[2:] if new_path.startswith("b/") else new_path
        old_normalized = old_path[2:] if old_path.startswith("a/") else old_path
        if path != old_normalized or path not in allowed:
            raise ValueError("patch path is not allowed: %s" % path)
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError("unsafe patch path")
        hunks = []
        while index < len(lines) and not lines[index].startswith("--- "):
            match = HUNK_HEADER.match(lines[index])
            if not match:
                if lines[index] == "\\ No newline at end of file":
                    index += 1
                    continue
                raise ValueError("invalid hunk header: %s" % lines[index][:100])
            old_start = int(match.group(1))
            old_count = int(match.group(2) or 1)
            new_start = int(match.group(3))
            new_count = int(match.group(4) or 1)
            index += 1
            body = []
            while index < len(lines):
                line = lines[index]
                if line.startswith("@@ ") or line.startswith("--- "):
                    break
                if not line.startswith((" ", "+", "-", "\\")):
                    raise ValueError("invalid unified patch line")
                if not line.startswith("\\"):
                    body.append(line)
                index += 1
            actual_old = sum(line[0] in {" ", "-"} for line in body)
            actual_new = sum(line[0] in {" ", "+"} for line in body)
            if actual_old != old_count or actual_new != new_count:
                raise ValueError("hunk line counts do not match header")
            hunks.append(Hunk(old_start, old_count, new_start, new_count, body))
        if not hunks:
            raise ValueError("file patch contains no hunks")
        patches.append(FilePatch(path, hunks))
    if not patches:
        raise ValueError("model returned no valid file patches")
    if len({item.path for item in patches}) != len(patches):
        raise ValueError("a file may appear only once in a generated patch")
    return patches


def apply_file_patch(content: str, patch: FilePatch) -> str:
    source = content.replace("\r\n", "\n").splitlines()
    output = []
    cursor = 0
    for hunk in patch.hunks:
        target = hunk.old_start - 1
        if target < cursor or target > len(source):
            raise ValueError("overlapping or out-of-range patch hunk")
        output.extend(source[cursor:target])
        cursor = target
        for line in hunk.lines:
            prefix, text = line[0], line[1:]
            if prefix in {" ", "-"}:
                if cursor >= len(source) or source[cursor] != text:
                    raise ValueError("patch context does not match source at line %d" % (cursor + 1))
                if prefix == " ":
                    output.append(text)
                cursor += 1
            elif prefix == "+":
                output.append(text)
    output.extend(source[cursor:])
    trailing = "\n" if content.endswith(("\n", "\r\n")) else ""
    return "\n".join(output) + trailing


class VerifiedPatchFixer:
    def __init__(self, client: JsonChatClient, verifier: RepairVerifier):
        self.client = client
        self.verifier = verifier

    def create_fix_commits(
        self, client, repository: str, pull_request: int, report: dict,
        workflow_state: Optional[Dict[str, Any]] = None,
        persist: Optional[Callable[[Dict[str, Any], bool], None]] = None,
        task_id: str = "", tenant_id: str = "default",
        reviewed_revision: str = "", task_created_at: str = "",
    ) -> dict:
        state = dict(workflow_state or {})
        report_hash = self._hash_json(report)

        def save(phase: str, completed: bool = False, **updates) -> None:
            state.update(updates)
            state["phase"] = phase
            if persist:
                persist(dict(state), completed)

        def terminal(phase: str, result: Dict[str, Any]) -> Dict[str, Any]:
            save(phase, True, result=result)
            return result

        if state:
            expected = {
                "task_id": task_id, "tenant": tenant_id, "repository": repository,
                "pull_request": pull_request, "report_hash": report_hash,
            }
            for key, value in expected.items():
                if state.get(key) != value:
                    raise ValueError("AutoFix checkpoint identity mismatch: %s" % key)
            if state.get("phase") in {"BLOCKED", "SUGGESTION_ONLY", "STALE_SOURCE"}:
                return dict(state["result"])
        else:
            pull = client.get_pull_request(repository, pull_request)
            source_sha = self._revision((pull.get("head") or {}).get("sha"), "pull request head")
            head_repository = (
                (pull.get("head") or {}).get("repo", {}).get("full_name") or repository
            )
            base_branch = str((pull.get("base") or {}).get("ref") or "main")
            suffix = re.sub(r"[^0-9A-Za-z]", "", task_id) or report_hash[:32]
            repair_branch = "evoagent/fix-%s" % suffix.lower()
            state.update({
                "task_id": task_id, "tenant": tenant_id, "repository": repository,
                "pull_request": pull_request, "head_repository": head_repository,
                "source_sha": source_sha, "base_branch": base_branch,
                "repair_branch": repair_branch, "report_hash": report_hash,
                "commit_timestamp": task_created_at,
            })
            if reviewed_revision and source_sha != reviewed_revision.lower():
                return terminal("STALE_SOURCE", self._stale_result(
                    reviewed_revision.lower(), source_sha,
                    "source_pr_updated_after_review",
                ))
            save("SOURCE_BOUND")

        phase = str(state.get("phase", ""))
        if phase == "PR_CREATED":
            self._verify_pull_request(
                client.get_pull_request(repository, int(state["pr_number"])), state,
            )
            return dict(state["result"])

        source_sha = self._revision(state.get("source_sha"), "checkpoint source_sha")
        head_repository = str(state["head_repository"])
        branch = str(state["repair_branch"])
        base_branch = str(state["base_branch"])
        paths = list(state.get("allowed_paths") or sorted({
            str(item.get("path")) for item in report.get("findings", [])
            if item.get("path") and (item.get("gate") or {}).get("passed", True)
        }))
        if not paths:
            return terminal("SUGGESTION_ONLY", {
                "status": "suggestion-only", "branch": None, "commits": [],
                "note": "No verified finding is eligible for patch generation.",
            })

        originals = self._load_originals(client, head_repository, paths, source_sha)
        generation = dict(state.get("generation") or {})
        if phase == "SOURCE_BOUND":
            ledger = ExecutionLedger("fix-agent")
            generated = self.client.complete_json(
                "fix-agent", PATCH_PROMPT,
                json.dumps({
                    "allowed_paths": paths, "findings": report.get("findings", []),
                    "files": originals,
                }, ensure_ascii=False), ledger, 8000,
            )
            patch_text = str(generated.get("patch", ""))
            generation = {
                "behavioral_claims": generated.get("behavioral_claims") or [],
                "related_tests": generated.get("related_tests") or [],
                "execution": ledger.summary(),
            }
            if not patch_text.strip():
                return terminal("SUGGESTION_ONLY", {
                    "status": "suggestion-only", "branch": None, "commits": [],
                    "reason": str(generated.get("reason", "insufficient evidence"))[:1000],
                    "execution": ledger.summary(),
                    "note": "The Fix Agent did not produce an evidence-backed patch.",
                })
            changed = self._apply_patch(patch_text, paths, originals)
            save(
                "PATCH_READY", patch=patch_text,
                patch_sha256=self._hash_text(patch_text),
                changed_sha256=self._hash_json(changed),
                allowed_paths=paths, generation=generation,
            )
            phase = "PATCH_READY"
        else:
            patch_text = str(state.get("patch", ""))
            if self._hash_text(patch_text) != state.get("patch_sha256"):
                raise ValueError("persisted AutoFix patch hash does not match")
            changed = self._apply_patch(patch_text, paths, originals)
            if self._hash_json(changed) != state.get("changed_sha256"):
                raise ValueError("reconstructed AutoFix content hash does not match")

        if phase == "PATCH_READY":
            structural = self.verifier.verify_contents(changed)
            if not structural["passed"]:
                return terminal("BLOCKED", {
                    "status": "blocked", "branch": None, "commits": [],
                    "patch": patch_text, "verification": {"structural": structural},
                    "execution": generation.get("execution", {}),
                    "note": "Patch failed AST/CST or compilation checks.",
                })
            if not self.verifier.test_command:
                return terminal("SUGGESTION_ONLY", {
                    "status": "suggestion-only", "branch": None, "commits": [],
                    "patch": patch_text, "verification": {"structural": structural},
                    "execution": generation.get("execution", {}),
                    "note": "No repository test command is configured; this is a suggestion, not a successful automatic fix.",
                })
            archive = client.download_archive(head_repository, source_sha)
            baseline = self.verifier.verify_archive(archive, {})
            patched = self.verifier.verify_archive(archive, changed)
            comparison = self.verifier.compare(baseline, patched)
            verification = {
                "structural": structural, "before": baseline,
                "after": patched, "comparison": comparison,
            }
            if not comparison["passed"]:
                return terminal("BLOCKED", {
                    "status": "blocked", "branch": None, "commits": [],
                    "patch": patch_text, "verification": verification,
                    "execution": generation.get("execution", {}),
                    "note": "Patch was blocked by before/after sandbox verification.",
                })
            save(
                "VERIFIED", patch_sha256=state["patch_sha256"],
                source_sha=source_sha, verification=verification,
            )
            phase = "VERIFIED"
        else:
            verification = dict(state.get("verification") or {})
            if state.get("source_sha") != source_sha:
                raise ValueError("persisted AutoFix verification source does not match")

        message = "fix: apply verified EvoAgent patch for PR #%d" % pull_request
        if phase == "VERIFIED":
            # Best-effort write guard. GitHub provides no transaction spanning
            # this lookup and the following Git object/ref/PR writes.
            current = client.get_pull_request(repository, pull_request)
            current_sha = self._revision(
                (current.get("head") or {}).get("sha"), "current pull request head",
            )
            if current_sha != source_sha:
                return terminal("STALE_SOURCE", self._stale_result(
                    source_sha, current_sha, "source_pr_updated_before_fix_publication",
                ))
            identity = {
                "name": "EvoAgent", "email": "evoagent@users.noreply.github.com",
                "date": str(state.get("commit_timestamp") or "1970-01-01T00:00:00+00:00"),
            }
            commit = client.create_commit_object(
                repository, source_sha, changed, message, identity,
            )
            save(
                "COMMIT_CREATED", commit_sha=str(commit["sha"]),
                tree_sha=str((commit.get("tree") or {}).get("sha", "")),
            )
            phase = "COMMIT_CREATED"

        commit_sha = str(state["commit_sha"])
        commit = client.get_git_commit(repository, commit_sha)
        self._verify_commit(commit, state)

        if phase == "COMMIT_CREATED":
            existing_branch = client.get_branch(repository, branch)
            if existing_branch is None:
                try:
                    client.create_branch_once(repository, branch, commit_sha)
                except Exception:
                    existing_branch = client.get_branch(repository, branch)
                    if existing_branch is None:
                        raise
            if existing_branch is None:
                existing_branch = client.get_branch(repository, branch)
            self._verify_branch(existing_branch, commit_sha)
            save("BRANCH_PUBLISHED")
            phase = "BRANCH_PUBLISHED"
        else:
            self._verify_branch(client.get_branch(repository, branch), commit_sha)

        if phase == "BRANCH_PUBLISHED":
            draft = client.find_pull_request(repository, branch, base_branch)
            if draft is None:
                try:
                    draft = client.create_draft_pull_request_once(
                        repository, "fix: verified EvoAgent patch for #%d" % pull_request,
                        branch, base_branch,
                        "LLM-generated patch. AST/CST, compilation and configured tests passed in an isolated checkout. This PR is intentionally a draft.",
                    )
                except Exception:
                    draft = client.find_pull_request(repository, branch, base_branch)
                    if draft is None:
                        raise
            self._verify_pull_request(draft, state)
            result = {
                "status": "verified-draft", "branch": branch, "source_sha": source_sha,
                "commits": [{"sha": commit_sha, "paths": sorted(changed)}],
                "draft_pull_request": {
                    "number": draft.get("number"), "url": draft.get("html_url"),
                },
                "patch": patch_text,
                "behavioral_claims": generation.get("behavioral_claims") or [],
                "related_tests": generation.get("related_tests") or [],
                "verification": verification,
                "execution": generation.get("execution", {}),
                "note": "Verified patch was published only as a draft pull request.",
            }
            save(
                "PR_CREATED", True, pr_number=int(draft["number"]),
                pr_url=draft.get("html_url"), result=result,
            )
            return result
        raise ValueError("unsupported AutoFix checkpoint phase: %s" % phase)

    @staticmethod
    def _hash_text(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _hash_json(value: Any) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _revision(value: Any, field: str) -> str:
        revision = str(value or "").strip().lower()
        if len(revision) not in {40, 64} or any(
            character not in "0123456789abcdef" for character in revision
        ):
            raise ValueError("%s must be a full hexadecimal Git revision" % field)
        return revision

    @staticmethod
    def _load_originals(client, repository: str, paths: List[str], source_sha: str) -> dict:
        return {
            path: client.get_file(repository, path, source_sha)["decoded_content"]
            for path in paths
        }

    @staticmethod
    def _apply_patch(patch_text: str, paths: List[str], originals: dict) -> dict:
        files = dict(originals)
        for patch in parse_unified_patch(patch_text, paths):
            files[patch.path] = apply_file_patch(originals[patch.path], patch)
        changed = {
            path: content for path, content in files.items()
            if content != originals[path]
        }
        if not changed:
            raise ValueError("generated patch makes no change")
        return changed

    @staticmethod
    def _stale_result(reviewed: str, current: str, reason: str) -> dict:
        return {
            "status": "stale-source", "branch": None, "commits": [],
            "reviewed_revision": reviewed, "current_revision": current,
            "remote_write": False, "reason": reason,
            "note": "AutoFix did not publish because the pull request source changed.",
        }

    @staticmethod
    def _verify_commit(commit: dict, state: dict) -> None:
        if str(commit.get("sha", "")) != str(state.get("commit_sha", "")):
            raise ValueError("AutoFix commit lookup returned an unexpected commit")
        parents = [str(item.get("sha", "")) for item in commit.get("parents", [])]
        if parents and parents != [str(state["source_sha"])]:
            raise ValueError("AutoFix commit has an unexpected parent")
        tree_sha = str(state.get("tree_sha", ""))
        if tree_sha and str((commit.get("tree") or {}).get("sha", "")) != tree_sha:
            raise ValueError("AutoFix commit has an unexpected tree")

    @staticmethod
    def _verify_branch(value: Optional[dict], commit_sha: str) -> None:
        if value is None:
            raise ValueError("AutoFix repair branch is missing")
        if str((value.get("object") or {}).get("sha", "")) != commit_sha:
            raise ValueError("AutoFix repair branch points to an unexpected commit")

    @staticmethod
    def _verify_pull_request(value: dict, state: dict) -> None:
        if str((value.get("head") or {}).get("ref", "")) != str(state["repair_branch"]):
            raise ValueError("AutoFix pull request has an unexpected head branch")
        if str((value.get("base") or {}).get("ref", "")) != str(state["base_branch"]):
            raise ValueError("AutoFix pull request has an unexpected base branch")


class SuggestionOnlyFixer:
    def create_fix_commits(
        self, client, repository, pull_request, report, workflow_state=None,
        persist=None, **_kwargs,
    ):
        state = dict(workflow_state or {})
        if state.get("phase") == "SUGGESTION_ONLY" and state.get("result"):
            return dict(state["result"])
        result = {
            "status": "suggestion-only", "branch": None, "commits": [],
            "suggestions": [item.get("fix", "") for item in report.get("findings", [])],
            "note": "No model is configured. Suggestions are not described as an automatic fix.",
        }
        state.update({"phase": "SUGGESTION_ONLY", "result": result})
        if persist:
            persist(state, True)
        return result
