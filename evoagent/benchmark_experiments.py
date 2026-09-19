"""Experiment-only policies and the frozen three-arm benchmark skeleton.

Nothing in this module is reachable from the production EvolutionRouter.  The
naive global policy is a control arm, not a production routing option.
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from .benchmark_governance import BenchmarkValidationError, build_run_manifest
from .evolution_lifecycle import CandidateLifecycle, EvolutionCandidate, EvolutionRouter


BENCHMARK_ARMS = (
    "baseline-no-evolution",
    "naive-global-evolution",
    "evidence-backed-targeted-evolution",
)


class NaiveGlobalBenchmarkPolicy:
    """Benchmark-only unconditional control route."""

    name = "naive-global-benchmark-only"

    @staticmethod
    def route(_attribution: Mapping[str, Any]) -> Dict[str, str]:
        return {"surface": "GLOBAL_PROMPT", "target_id": "llm-review"}


@dataclass
class ArmRuntime:
    arm: str
    starting_release_id: str
    ending_release_id: str
    lifecycle: CandidateLifecycle
    candidate_builder: Callable[[Mapping[str, Any], Mapping[str, str]], EvolutionCandidate]
    evaluator: Callable[[Dict[str, Any]], Dict[str, Any]]
    promoter: Callable[[str], Dict[str, Any]]
    active_release_reader: Callable[[], str]
    evaluation_policy_id: str


class ThreeArmBenchmarkRunner:
    """Run one frozen failure stream through isolated injected arm runtimes.

    The caller must build three independent Stores from the same starting
    snapshot.  This skeleton owns ordering and lifecycle use; it deliberately
    does not know how a model generates a Prompt or Skill change.
    """

    def __init__(
        self, arm_factory: Callable[[str], ArmRuntime], *, git_commit: str,
        dataset_manifest_sha256: str, model_identity: Mapping[str, Any],
        runtime_config: Mapping[str, Any], matcher_identity: Mapping[str, Any],
        metric_identity: Mapping[str, Any], evaluation_case_order: Sequence[str],
        random_seed: int = 20260819,
    ):
        self.arm_factory = arm_factory
        self.git_commit = git_commit
        self.dataset_manifest_sha256 = dataset_manifest_sha256
        self.model_identity = dict(model_identity)
        self.runtime_config = dict(runtime_config)
        self.matcher_identity = dict(matcher_identity)
        self.metric_identity = dict(metric_identity)
        self.evaluation_case_order = list(evaluation_case_order)
        self.random_seed = int(random_seed)

    @staticmethod
    def _route(arm: str, attribution: Mapping[str, Any]) -> Dict[str, str]:
        if arm == "baseline-no-evolution":
            return {"surface": "NO_SUPPORTED_EVOLUTION", "target_id": ""}
        if arm == "naive-global-evolution":
            return NaiveGlobalBenchmarkPolicy.route(attribution)
        if arm == "evidence-backed-targeted-evolution":
            return EvolutionRouter.route(dict(attribution))
        raise BenchmarkValidationError("unknown benchmark arm: %s" % arm)

    def run(self, failures: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        failure_ids = [str(item.get("failure_id") or "") for item in failures]
        if not all(failure_ids) or len(failure_ids) != len(set(failure_ids)):
            raise BenchmarkValidationError("failure stream requires unique stable failure_id values")
        arms = {}
        starting_release = None
        evaluation_policy = None
        stores = set()
        for arm in BENCHMARK_ARMS:
            runtime = self.arm_factory(arm)
            if runtime.arm != arm:
                raise BenchmarkValidationError("arm factory returned mismatched runtime")
            if starting_release is None:
                starting_release = runtime.starting_release_id
            elif runtime.starting_release_id != starting_release:
                raise BenchmarkValidationError("all arms must start from the same Release R0")
            if evaluation_policy is None:
                evaluation_policy = runtime.evaluation_policy_id
            elif runtime.evaluation_policy_id != evaluation_policy:
                raise BenchmarkValidationError("all evolution arms must share one evaluator/gate policy")
            store_identity = id(runtime.lifecycle.store)
            if store_identity in stores:
                raise BenchmarkValidationError("benchmark arms must use independent Store state")
            stores.add(store_identity)
            events = []
            for failure in failures:
                attribution = failure.get("attribution") or {}
                route = self._route(arm, attribution)
                if route["surface"] == "NO_SUPPORTED_EVOLUTION":
                    events.append({
                        "failure_id": str(failure["failure_id"]),
                        "route": route, "candidate_id": None,
                        "decision": "NO_CANDIDATE",
                    })
                    continue
                candidate = runtime.candidate_builder(failure, route)
                persisted = runtime.lifecycle.create(candidate)
                evaluated = runtime.lifecycle.evaluate(
                    persisted["candidate_id"], runtime.evaluator,
                )
                promotion = None
                if evaluated["status"] == "READY_FOR_PROMOTION":
                    promotion = runtime.promoter(evaluated["candidate_id"])
                events.append({
                    "failure_id": str(failure["failure_id"]), "route": route,
                    "candidate_id": evaluated["candidate_id"],
                    "decision": evaluated["status"], "promotion": promotion,
                })
            runtime.ending_release_id = runtime.active_release_reader()
            arms[arm] = {
                "events": events,
                "run_manifest": build_run_manifest(
                    git_commit=self.git_commit,
                    dataset_manifest_sha256=self.dataset_manifest_sha256,
                    arm=arm, starting_release_id=runtime.starting_release_id,
                    ending_release_id=runtime.ending_release_id,
                    model_identity=self.model_identity,
                    runtime_config=self.runtime_config,
                    matcher_identity=self.matcher_identity,
                    metric_identity=self.metric_identity,
                    failure_stream=failure_ids,
                    evaluation_case_order=self.evaluation_case_order,
                    random_seed=self.random_seed,
                    experiment_mode="CONTROLLED_SHARED_FAILURE_STREAM",
                ),
            }
        return {
            "schema_version": 2,
            "claim_scope": "BENCHMARK_RUNNER_SKELETON_NO_FINAL_RESULTS",
            "experiment_mode": "CONTROLLED_SHARED_FAILURE_STREAM",
            "candidate_budget_per_failure": 1,
            "promotion_limit_per_failure": 1,
            "evaluation_policy_id": evaluation_policy,
            "failure_stream": failure_ids,
            "failure_stream_identity": arms[BENCHMARK_ARMS[0]]["run_manifest"]["failure_stream_identity"],
            "arms": arms,
        }
