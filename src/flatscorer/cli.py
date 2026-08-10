"""The `flatscorer` console script, and `python -m flatscorer`."""

from __future__ import annotations

import argparse
import json
import os
import sys

from .config import DEFAULT_CONFIG, validate_config
from .scorer import FlatScorer


def _invocation() -> str:
    """How the user actually started this run, for accurate hint text.

    Installed via pip the entry point is `flatscorer`; from a checkout it's
    `python -m flatscorer`, which arrives here as the path to `__main__.py`.
    Telling someone to run the file they don't have is worse than no hint at all.
    """
    invoked_as = os.path.basename(sys.argv[0] or "")
    if invoked_as == "__main__.py":
        return f"python -m {__package__}"
    if invoked_as.endswith(".py"):
        return f"python {invoked_as}"
    return invoked_as or "flatscorer"


def main():
    parser = argparse.ArgumentParser(
        description="FlatScorer - Multi-Criteria Apartment Scoring Engine"
    )
    parser.add_argument(
        "-c", "--config", type=str, help="Path to JSON configuration file"
    )
    parser.add_argument(
        "--generate-config", type=str, help="Write default example config JSON to specified file and exit"
    )
    parser.add_argument(
        "--csv", type=str, help="Override output CSV file path"
    )
    parser.add_argument(
        "--html", type=str, help="Override output HTML map file path"
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress detailed logs"
    )

    args = parser.parse_args()

    if args.generate_config:
        with open(args.generate_config, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        print(f"[+] Generated sample configuration file at '{args.generate_config}'")
        print("    Edit it with your own addresses, then run:")
        print(f"    {_invocation()} --config {args.generate_config}")
        sys.exit(0)

    if not args.config:
        print("No config file specified. Running with built-in demo data.")
        print(f"To create your own config: {_invocation()} --generate-config config.json")
        print()

    config = DEFAULT_CONFIG
    if args.config:
        if not os.path.exists(args.config):
            sys.exit(f"Error: Config file '{args.config}' not found.")
        with open(args.config, encoding="utf-8") as f:
            try:
                config = json.load(f)
            except json.JSONDecodeError as e:
                sys.exit(f"Error: Config file '{args.config}' is not valid JSON: {e}")

    # Validate before touching the network, and report every problem at once so a
    # hand-edited config takes one fix-and-rerun cycle rather than one per typo.
    problems = validate_config(config)
    if problems:
        print(f"Error: configuration is not valid ({len(problems)} problem(s)):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        sys.exit(1)

    if args.csv:
        config.setdefault("output", {})["csv_file"] = args.csv
    if args.html:
        config.setdefault("output", {})["html_file"] = args.html

    scorer = FlatScorer(config, verbose=not args.quiet)
    scorer.run()
