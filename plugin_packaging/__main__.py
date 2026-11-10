"""python -m plugin_packaging {build|check|stage <dest>}"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .build import MARKETPLACE_NAME, PACK_ROOT, run, stage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m plugin_packaging",
                                     description="Package the Django Engineering Pack as a Claude Code plugin.")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("build", help="regenerate the derived plugin files from their canonical sources")
    sub.add_parser("check", help="report drift and structure problems without writing")
    stage_parser = sub.add_parser("stage", help="export an installable plugin folder with a local marketplace")
    stage_parser.add_argument("dest", type=Path, help="output folder outside the pack (replaced if it is a previous stage)")
    args = parser.parse_args(argv)

    if args.action == "stage":
        report = stage(args.dest)
    else:
        report = run(PACK_ROOT, write=args.action == "build")
    if args.action == "stage":
        print(f"staged {len(report.written)} files to {args.dest}" if report.ok else "nothing staged")
    else:
        for rel in report.written:
            print(f"wrote {rel}")
    for problem in report.problems:
        print(f"error: {problem}", file=sys.stderr)
    if report.ok and args.action == "stage":
        print(f"install: claude plugin marketplace add {args.dest.resolve()}\n"
              f"         claude plugin install django-engineering@{MARKETPLACE_NAME}")
    elif report.ok:
        print("plugin packaging is consistent with its canonical sources")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
