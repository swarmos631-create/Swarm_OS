"""SwarmOS command-line interface.

    swarmos init                    migrate + catalog + source registry + dev accounts + sample docs + demo data
    swarmos migrate                 apply database migrations
    swarmos seed [--no-demo]        re-run catalog/registry/docs/demo seeding (idempotent)
    swarmos create-user EMAIL ROLE  create an account (password read from prompt or SWARMOS_NEW_USER_PASSWORD)
    swarmos sync CITY [--source S]  synchronise live sources for a city now
    swarmos ml list | train NAME [--promote-if-better] | train-all | promote NAME VERSION [--force] | rollback NAME
    swarmos serve [--port 8000]     run the API (with the embedded worker unless disabled)
    swarmos worker                  run a standalone background worker
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from pathlib import Path

from swarmos.config import get_settings


def _alembic_cfg():  # type: ignore[no-untyped-def]
    from alembic.config import Config

    configured = os.environ.get("SWARMOS_ALEMBIC_CONFIG")
    candidates = [Path(configured)] if configured else []
    candidates.extend((
        Path(__file__).resolve().parents[1] / "alembic.ini",
        Path.cwd() / "backend" / "alembic.ini",
        Path.cwd() / "alembic.ini",
    ))
    for path in candidates:
        if path.is_file():
            return Config(str(path))
    searched = ", ".join(str(path) for path in candidates)
    raise FileNotFoundError(f"Alembic configuration not found; set SWARMOS_ALEMBIC_CONFIG. Searched: {searched}")


def cmd_migrate(_: argparse.Namespace) -> None:
    from alembic import command

    command.upgrade(_alembic_cfg(), "head")
    print("Database migrated to head.")


def cmd_seed(args: argparse.Namespace) -> None:
    from swarmos.db import session_scope
    from swarmos.demo.seed import bootstrap

    with session_scope() as db:
        out = bootstrap(db, get_settings(), demo=False if args.no_demo else None)
    cred = out.pop("credentials_file", None)
    print(json.dumps(out, indent=2, default=str))
    if cred:
        print(f"\nDevelopment accounts created. Passwords were written to {cred} (mode 600). They are not printed here.")


def cmd_init(args: argparse.Namespace) -> None:
    cmd_migrate(args)
    cmd_seed(args)


def cmd_create_user(args: argparse.Namespace) -> None:
    from swarmos.core.security import hash_password, validate_password_strength
    from swarmos.db import session_scope
    from swarmos.models import User

    password = os.environ.get("SWARMOS_NEW_USER_PASSWORD") or getpass.getpass("Password (min 12 chars, mixed case + digit): ")
    validate_password_strength(password)
    with session_scope() as db:
        db.add(User(email=args.email.lower(), display_name=args.name or args.email, role=args.role, all_cities=args.all_cities,
                    password_hash=hash_password(password)))
    print(f"Created {args.role} {args.email}")


def cmd_sync(args: argparse.Namespace) -> None:
    from swarmos.integrations.sync import sync_city

    res = asyncio.run(sync_city(args.city, [args.source] if args.source else None, force=True))
    print(json.dumps(res, indent=2, default=str))


def cmd_ml(args: argparse.Namespace) -> None:
    from swarmos.ml import pipeline

    s = get_settings()
    if args.ml_cmd == "list":
        for cfg in pipeline.available_configs(s):
            versions = pipeline.list_versions(cfg["model_name"])
            promoted = next((v.version for v in versions if v.status == "promoted"), "—")
            print(f"{cfg['model_name']:<28} versions={len(versions):<3} promoted={promoted}")
    elif args.ml_cmd == "train":
        print(json.dumps(pipeline.train_model(args.name, promote_if_better=args.promote_if_better), indent=2))
    elif args.ml_cmd == "train-all":
        for cfg in pipeline.available_configs(s):
            r = pipeline.train_model(cfg["model_name"], promote_if_better=args.promote_if_better)
            print(f"{r['model_name']:<28} {r['version']}  gate={'PASS' if r['gate_passed'] else 'FAIL'}  promoted={r['promoted']}  {r['metrics']}")
    elif args.ml_cmd == "promote":
        pipeline.promote(args.name, args.version, force=args.force)
        print(f"Promoted {args.name}@{args.version}")
    elif args.ml_cmd == "rollback":
        print(f"Rolled back {args.name} to {pipeline.rollback(args.name)}")


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("swarmos.main:app", host=args.host, port=args.port, reload=False, proxy_headers=True, log_config=None)


def cmd_worker(_: argparse.Namespace) -> None:
    from swarmos.jobs.worker import main as worker_main

    worker_main()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="swarmos", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("init", cmd_init), ("migrate", cmd_migrate), ("seed", cmd_seed)):
        sp = sub.add_parser(name)
        sp.add_argument("--no-demo", action="store_true")
        sp.set_defaults(fn=fn)
    sp = sub.add_parser("create-user")
    sp.add_argument("email")
    sp.add_argument("role", choices=["viewer", "operator", "admin"])
    sp.add_argument("--name")
    sp.add_argument("--all-cities", action="store_true")
    sp.set_defaults(fn=cmd_create_user)
    sp = sub.add_parser("sync")
    sp.add_argument("city")
    sp.add_argument("--source")
    sp.set_defaults(fn=cmd_sync)
    ml = sub.add_parser("ml")
    mls = ml.add_subparsers(dest="ml_cmd", required=True)
    mls.add_parser("list")
    t = mls.add_parser("train")
    t.add_argument("name")
    t.add_argument("--promote-if-better", action="store_true")
    ta = mls.add_parser("train-all")
    ta.add_argument("--promote-if-better", action="store_true")
    pr = mls.add_parser("promote")
    pr.add_argument("name")
    pr.add_argument("version")
    pr.add_argument("--force", action="store_true")
    rb = mls.add_parser("rollback")
    rb.add_argument("name")
    ml.set_defaults(fn=cmd_ml)
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(fn=cmd_serve)
    sub.add_parser("worker").set_defaults(fn=cmd_worker)
    args = p.parse_args(argv)
    try:
        args.fn(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
