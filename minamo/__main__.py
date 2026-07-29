"""Minamo runnable entry point.

Launch with:  python -m minamo            # scaffolds config/ on first run, then serves
            python -m minamo init         # only scaffold config/ and exit
            python -m minamo run [...]    # same as the default, with CLI overrides

Precedence is CLI > ENV (MINAMO_*) > CONFIG (config/*.toml).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from .app import create_app
from .config import ConfigManager, DEFAULT_CONFIG_DIR
from .config.scaffold import scaffold_config

# Maps CLI flags to dotted paths inside the raw config dict.
_OVERRIDE_FLAGS: dict[str, list[str]] = {
    "backend": ["app", "backend"],
    "endpoint_host": ["app", "endpoint_host"],
    "region": ["app", "region"],
    "enforce_signature": ["app", "enforce_signature"],
    "presign_ttl": ["app", "presign_ttl"],
    "access_key": ["secrets", "access_key"],
    "secret_key": ["secrets", "secret_key"],
    "data_root": ["app", "data", "root"],
}


def _add_override_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config-dir", default=None, help="Config directory (default: ./config)")
    p.add_argument("--backend", default=None)
    p.add_argument("--endpoint-host", default=None)
    p.add_argument("--region", default=None)
    p.add_argument("--enforce-signature", default=None)
    p.add_argument("--presign-ttl", default=None, type=int)
    p.add_argument("--access-key", default=None)
    p.add_argument("--secret-key", default=None)
    p.add_argument("--data-root", default=None)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minamo")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init", help="Scaffold the config/ directory and exit.")
    run = sub.add_parser("run", help="Run the server (default).")
    _add_override_args(run)
    _add_override_args(parser)
    return parser


def _collect_overrides(args: argparse.Namespace) -> dict:
    overrides: dict = {}
    for flag, path in _OVERRIDE_FLAGS.items():
        value = getattr(args, flag, None)
        if value is not None:
            overrides[".".join(path)] = value
    return overrides


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = args.command or "run"
    config_dir = Path(args.config_dir) if args.config_dir else DEFAULT_CONFIG_DIR

    if command == "init":
        if config_dir.is_dir():
            print(f"[minamo] config directory already exists: {config_dir}")
        else:
            scaffold_config(config_dir)
            print(
                f"[minamo] config scaffolded at: {config_dir}\n"
                f"[minamo] edit the files (especially secrets.toml), then run: "
                f"python -m minamo"
            )
        return

    # run
    config = ConfigManager(
        config_dir=config_dir,
        overrides=_collect_overrides(args),
        exit_on_missing=True,
    )
    app = create_app(config)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")


if __name__ == "__main__":
    main()
