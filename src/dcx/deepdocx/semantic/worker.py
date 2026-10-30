"""System semantic-index worker entry point."""

import argparse
from pathlib import Path

from .service import run_worker

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run_worker(args.settings))
