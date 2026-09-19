"""Freeze selected public GitHub PRs into self-contained read-only Git snapshots."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import subprocess
import tempfile
from typing import Any, Dict


def run(*args: str, text: bool = True) -> Any:
    return subprocess.check_output(list(args), text=text)


def gh_json(path: str) -> Any:
    return json.loads(run("gh", "api", path))


def write_bytes(path: str, value: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(value)


def write_json(path: str, value: Any) -> None:
    write_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())


def snapshot_commit(git_dir: str, tree_sha: str, message: str) -> str:
    environment = dict(os.environ)
    environment.update({
        "GIT_AUTHOR_NAME": "EvoAgent benchmark capture",
        "GIT_AUTHOR_EMAIL": "benchmark-capture@invalid.local",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
        "GIT_COMMITTER_NAME": "EvoAgent benchmark capture",
        "GIT_COMMITTER_EMAIL": "benchmark-capture@invalid.local",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    })
    return subprocess.check_output(
        ["git", "--git-dir", git_dir, "commit-tree", tree_sha],
        input=message + "\n", text=True, env=environment,
    ).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("selection")
    parser.add_argument("output")
    args = parser.parse_args()
    with open(args.selection, encoding="utf-8") as handle:
        selection = json.load(handle)
    dry_run_iteration = int(selection.get("dry_run_iteration", 1))
    guide_version = str(selection.get("annotation_guide_version", "evidence-adjudication-guide-v1"))
    captured = []
    sources = os.path.join(args.output, "sources")
    cases_root = os.path.join(args.output, "cases")
    os.makedirs(sources, exist_ok=True)
    temporary_sources = tempfile.TemporaryDirectory(prefix="evoagent-dry-run-capture-")
    for chosen in selection["cases"]:
        repo, number, case_id = chosen["repository"], int(chosen["pull_request"]), chosen["case_id"]
        pr = gh_json("repos/%s/pulls/%d" % (repo, number))
        comparison = gh_json(
            "repos/%s/compare/%s...%s" % (repo, pr["base"]["sha"], pr["head"]["sha"])
        )
        review_base_sha = comparison["merge_base_commit"]["sha"]
        files = gh_json("repos/%s/pulls/%d/files?per_page=100" % (repo, number))
        license_info: Dict[str, Any]
        try:
            license_raw = gh_json("repos/%s/license?ref=%s" % (repo, pr["base"]["sha"]))
            license_info = {
                "name": license_raw.get("license", {}).get("name"),
                "spdx_id": license_raw.get("license", {}).get("spdx_id"),
                "path": license_raw.get("path"),
                "html_url": license_raw.get("html_url"),
            }
        except subprocess.CalledProcessError:
            repository_info = gh_json("repos/%s" % repo)
            detected = repository_info.get("license") or {}
            license_info = {
                "name": detected.get("name"), "spdx_id": detected.get("spdx_id"),
                "path": None, "html_url": detected.get("url"),
                "note": "Repository-level GitHub license metadata; no license file detected at frozen base revision.",
            }
        diff = subprocess.check_output([
            "gh", "api", "-H", "Accept: application/vnd.github.v3.diff",
            "repos/%s/pulls/%d" % (repo, number),
        ])
        case_dir = os.path.join(cases_root, case_id)
        write_bytes(os.path.join(case_dir, "diff.patch"), diff)
        metadata = {
            "schema_version": 1, "case_id": case_id, "repository_id": repo,
            "source": {"kind": "public-github-pr", "reference_id": "%s#%d" % (repo, number), "url": pr["html_url"]},
            "pull_request": number, "title": pr["title"], "author": pr["user"]["login"],
            "body": pr.get("body") or "", "merged_at": pr["merged_at"],
            "base_sha": review_base_sha, "head_sha": pr["head"]["sha"],
            "repository_base_tip_sha_at_capture": pr["base"]["sha"],
            "base_identity_method": "github-compare-merge-base",
            "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "diff_sha256": hashlib.sha256(diff).hexdigest(),
            "changed_files": [{
                "path": item["filename"], "status": item["status"],
                "additions": item["additions"], "deletions": item["deletions"],
                "previous_filename": item.get("previous_filename"),
            } for item in files],
            "license": license_info,
            "requested_sampling_category": chosen.get("requested_sampling_category", chosen.get("coverage")),
            "coverage": chosen.get("coverage", chosen.get("requested_sampling_category")),
            "sampling_rationale": chosen["sampling_rationale"],
            "protocol_dry_run": True, "final_claim_eligible": False,
            "dry_run_iteration": dry_run_iteration,
            "annotation_guide_version": guide_version,
        }
        write_json(os.path.join(case_dir, "metadata.json"), metadata)
        bare = os.path.join(temporary_sources.name, repo.replace("/", "__") + ".git")
        if not os.path.exists(bare):
            subprocess.check_call(["git", "init", "--bare", "--quiet", bare])
        remote = "https://github.com/%s.git" % repo
        subprocess.check_call([
            "git", "-C", bare, "fetch", "--quiet", "--depth=1", remote,
            "%s:refs/dryrun/%s/base" % (review_base_sha, case_id),
            "refs/pull/%d/head:refs/dryrun/%s/head" % (number, case_id),
        ])
        actual_head = run("git", "-C", bare, "rev-parse", "refs/dryrun/%s/head" % case_id).strip()
        if actual_head != pr["head"]["sha"]:
            raise RuntimeError("captured head mismatch for %s" % case_id)
        metadata["base_tree_sha"] = run("git", "-C", bare, "rev-parse", review_base_sha + "^{tree}").strip()
        metadata["head_tree_sha"] = run("git", "-C", bare, "rev-parse", pr["head"]["sha"] + "^{tree}").strip()
        write_json(os.path.join(case_dir, "metadata.json"), metadata)
        captured.append(metadata)
    for repository_id in sorted({item["repository_id"] for item in captured}):
        bare = os.path.join(temporary_sources.name, repository_id.replace("/", "__") + ".git")
        bundle = os.path.abspath(os.path.join(sources, repository_id.replace("/", "__") + ".bundle"))
        snapshot_refs = []
        for item in (entry for entry in captured if entry["repository_id"] == repository_id):
            for side in ("base", "head"):
                reference = "refs/snapshots/%s/%s" % (item["case_id"], side)
                commit = snapshot_commit(
                    bare, item[side + "_tree_sha"],
                    "Frozen tree for %s original %s %s" % (
                        item["case_id"], side, item[side + "_sha"],
                    ),
                )
                subprocess.check_call(["git", "-C", bare, "update-ref", reference, commit])
                item[side + "_snapshot_ref"] = reference
                item[side + "_snapshot_commit"] = commit
                write_json(os.path.join(cases_root, item["case_id"], "metadata.json"), item)
                snapshot_refs.append(reference)
        subprocess.check_call(["git", "-C", bare, "bundle", "create", bundle] + snapshot_refs)
        subprocess.check_call(["git", "bundle", "verify", bundle], stdout=subprocess.DEVNULL)
    temporary_sources.cleanup()
    write_json(os.path.join(args.output, "capture_manifest.json"), {
        "dataset_id": selection["dataset_id"], "dataset_version": selection["dataset_version"],
        "protocol_dry_run": True, "final_claim_eligible": False,
        "dry_run_iteration": dry_run_iteration,
        "case_count": len(captured), "repository_count": len({item["repository_id"] for item in captured}),
        "cases": captured,
    })


if __name__ == "__main__":
    main()
