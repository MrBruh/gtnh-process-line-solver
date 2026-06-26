"""Smoke tests so CI is green from the first commit.

Real coverage (property tests, golden corpus, per-module tests) lands with each module -
see docs/TESTING.md.
"""

from gtnh_solver import __version__
from gtnh_solver.cli import main


def test_version_is_nonempty_string() -> None:
    assert isinstance(__version__, str)
    assert __version__


def test_cli_main_is_callable() -> None:
    # with no export path the CLI returns a clean usage error (exit 2), not a crash;
    # the real solve/output flows are covered in test_cli.py
    assert main([]) == 2
