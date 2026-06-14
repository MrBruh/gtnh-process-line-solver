"""cli — the `gtnh-solve` entry point.

Planned: parse a gtnh-flow project, run the solver, emit the previewer JSON + build guide,
honor per-commodity ME flags, and surface infeasibility clearly. Not implemented yet; this
stub wires the entry point and `--version` so the package is installable and CI is green.
"""

from __future__ import annotations

import argparse

from gtnh_solver import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gtnh-solve",
        description="Physical place-and-route solver for GregTech: New Horizons.",
    )
    parser.add_argument("--version", action="version", version=f"gtnh-solve {__version__}")
    parser.add_argument("project", nargs="?", help="path to a gtnh-flow project (planned)")
    parser.add_argument("--out", default="out/", help="output directory (planned)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(
        "gtnh-solve is in planning/pre-alpha - the solver is not implemented yet.\n"
        "See docs/ROADMAP.md for the build order and CONTRIBUTING.md to help.\n"
        f"(requested project={args.project!r}, out={args.out!r})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
