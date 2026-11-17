"""Codegrinder's stable, hierarchical command-line entry point."""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

from .engine import Controller, StopWorkflow, single_runner
from .config import load_config, ConfigurationError
from .roadmap import RoadmapFormatError


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="grind",
        description="Run a versioned Markdown PR roadmap through Codex planning/review and Pi coding.",
        epilog="Run 'grind run --help' for execution options."
    )
    root.add_argument("--conf", type=Path, metavar="PATH",
                      help="Optional TOML configuration (default: ./grind.toml when present)")
    commands = root.add_subparsers(dest="command", metavar="COMMAND", required=True)
    run = commands.add_parser(
        "run", help="Execute or resume the full PR roadmap",
        description="Execute the complete, ordered roadmap. State is retained outside the Git checkout."
    )
    run.add_argument("--conf", dest="run_conf", type=Path, metavar="PATH",
                     help="Optional TOML configuration (also accepted before run)")
    run.add_argument("-g", "--guide", type=Path, required=True, metavar="PATH",
                     help="Detailed PR plan authoring guide (Markdown, absolute or relative to CWD)")
    run.add_argument("-r", "--roadmap", type=Path, required=True, metavar="PATH",
                     help="Versioned PR roadmap (Markdown, absolute or relative to CWD)")
    return root


def _repo_root() -> Path:
    completed = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                               capture_output=True, text=True, check=False)
    if completed.returncode or not completed.stdout.strip():
        raise StopWorkflow("REPO_INVALID: run 'grind' within the target Git checkout")
    return Path(completed.stdout.strip()).resolve()


def state_directory(repo: Path, roadmap: Path) -> Path:
    """Stable per-repo+roadmap state path under XDG_STATE_HOME or ~/.local/state."""
    home = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))).expanduser()
    key = hashlib.sha256(f"{repo}\n{roadmap}".encode("utf-8")).hexdigest()[:20]
    return home / "codegrinder" / key


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    os.umask(0o077)
    try:
        repo = _repo_root()
        guide, roadmap = args.guide.expanduser().resolve(), args.roadmap.expanduser().resolve()
        if not guide.is_file():
            raise StopWorkflow(f"GUIDE_MISSING: {guide}")
        if not roadmap.is_file():
            raise StopWorkflow(f"ROADMAP_MISSING: {roadmap}")
        if args.conf is not None and args.run_conf is not None:
            raise ConfigurationError("CONFIG_DUPLICATE: pass --conf only once")
        configuration = load_config(args.conf if args.conf is not None else args.run_conf)
        planner, coder, reviewer = configuration.adapters()
        state_dir = state_directory(repo, roadmap)
        with single_runner(state_dir):
            ctrl = Controller(
                repo, roadmap, state_dir, guide,
                planner=planner, coder=coder, reviewer=reviewer,
                configuration_sha256=configuration.sha256,
            )
            return ctrl.process(follow=True, max_wait_minutes=None)
    except (StopWorkflow, RoadmapFormatError, ConfigurationError, FileNotFoundError, OSError, ValueError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("INTERRUPTED: resume by running the same command", file=sys.stderr)
        return 130
