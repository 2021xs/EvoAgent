"""Regenerate the checked-in synthetic prompt-evolution corpus."""

import argparse
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evoagent.evolution_proof import (  # noqa: E402
    DEFAULT_PROMPT_DATASET,
    build_prompt_evolution_cases,
    write_jsonl,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate the deterministic synthetic prompt-evolution corpus."
    )
    parser.add_argument("--output", default=DEFAULT_PROMPT_DATASET)
    args = parser.parse_args()
    cases = build_prompt_evolution_cases()
    write_jsonl(cases, args.output)
    print("wrote %d cases to %s" % (len(cases), os.path.abspath(args.output)))


if __name__ == "__main__":
    main()
