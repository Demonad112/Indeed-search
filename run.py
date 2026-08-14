#!/usr/bin/env python3
"""jobpipe CLI.

    python run.py discover [--since 7d] [--source indeed] [--dry-run] [-v]
    python run.py status
    python run.py probe --greenhouse <slug> | --lever <slug> | --ashby <slug>

Later phases (score / draft / serve / submit) are registered but not built yet;
they exit non-zero with a pointer rather than pretending to work.
"""

from __future__ import annotations

import argparse
import sys

from jobpipe import log as logging_setup


def _not_built(phase: str, arg: str) -> int:
    print(
        f"'{arg}' is Phase {phase} and is not built yet.\n"
        f"Phase 1 (discover) is the current phase — run `python run.py discover`.",
        file=sys.stderr,
    )
    return 1


def cmd_discover(args: argparse.Namespace) -> int:
    from jobpipe import discover

    return discover.run(
        since=args.since,
        only=args.source,
        dry_run=args.dry_run,
        apply_title_filter=not args.no_title_filter,
    )


def cmd_status(args: argparse.Namespace) -> int:
    import json

    from jobpipe.config import load_settings
    from jobpipe.db import open_db

    settings = load_settings()
    conn = open_db(settings.db_path)

    print(f"database: {settings.db_path}")

    rows = conn.execute(
        "SELECT status, COUNT(*) n FROM jobs GROUP BY status ORDER BY n DESC"
    ).fetchall()
    total = sum(r["n"] for r in rows)
    print(f"\njobs ({total} total)")
    for row in rows:
        print(f"  {row['n']:>4}  {row['status']}")
    if not rows:
        print("  (none — run `python run.py discover`)")

    runs = conn.execute(
        "SELECT id, started_at, finished_at, exit_code, sources_ok, sources_failed, stats "
        "FROM runs ORDER BY id DESC LIMIT 5"
    ).fetchall()
    if runs:
        print("\nrecent runs")
        for r in runs:
            stats = json.loads(r["stats"] or "{}")
            failed = json.loads(r["sources_failed"] or "[]")
            flag = "" if r["exit_code"] == 0 else f"  exit={r['exit_code']} failed={','.join(failed)}"
            print(
                f"  #{r['id']:<4} {r['started_at']}  "
                f"+{stats.get('inserted', 0)} ~{stats.get('updated', 0)} "
                f"-{stats.get('skipped', 0)} x{stats.get('expired', 0)}{flag}"
            )

    skips = conn.execute(
        "SELECT reason, COUNT(*) n FROM skips GROUP BY reason ORDER BY n DESC LIMIT 10"
    ).fetchall()
    if skips:
        print("\nskip reasons (all time)")
        for s in skips:
            print(f"  {s['n']:>4}  {s['reason']}")

    conn.close()
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """Sanity-check a board slug before committing it to sources.yaml."""
    from jobpipe.config import load_settings
    from jobpipe.net import FetchError, Http
    from jobpipe.sources import AshbySource, GreenhouseSource, LeverSource

    settings = load_settings()
    pairs = [
        (GreenhouseSource, args.greenhouse, "greenhouse"),
        (LeverSource, args.lever, "lever"),
        (AshbySource, args.ashby, "ashby"),
    ]
    chosen = [(cls, slug, name) for cls, slug, name in pairs if slug]
    if not chosen:
        print("give one of --greenhouse/--lever/--ashby SLUG", file=sys.stderr)
        return 2

    rc = 0
    with Http(settings) as http:
        for cls, slug, name in chosen:
            print(f"\n--- {name}: {slug} ---")
            try:
                postings = list(cls(http, [{"slug": slug, "name": slug}]).fetch())
            except (FetchError, ValueError) as exc:
                print(f"FAILED: {exc}", file=sys.stderr)
                rc = 1
                continue
            print(f"{len(postings)} posting(s)")
            src = cls(http, [{"slug": slug, "name": slug}])
            for p in postings[:5]:
                # Boards that fetch descriptions lazily (Ashby) need the second
                # request before there is anything to show.
                if p.description_raw is None and hasattr(src, "fetch_details"):
                    try:
                        src.fetch_details(p)
                    except (FetchError, ValueError) as exc:
                        print(f"    (detail fetch failed: {exc})")
                desc = len(p.description_raw or "")
                pay = p.salary_text or "-"
                print(f"  · {p.title[:46]:<46} {p.location[:24]:<24} desc={desc}b")
                if p.salary_text:
                    print(f"      pay: {pay[:70]}")
            if len(postings) > 5:
                print(f"  … and {len(postings) - 5} more (showing 5 to limit detail requests)")
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run.py", description="jobpipe")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p_disc = sub.add_parser("discover", help="Phase 1 — pull postings from all sources")
    p_disc.add_argument("--since", help="only ingest postings newer than this (ISO date or 7d/36h/2w)")
    p_disc.add_argument(
        "--source", action="append", help="restrict to one source (repeatable)"
    )
    p_disc.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    p_disc.add_argument(
        "--no-title-filter",
        action="store_true",
        help="ingest postings that match criteria.yaml exclude.titles "
        "(needed to calibrate Phase 2 scoring against deliberately-low postings)",
    )
    p_disc.set_defaults(func=cmd_discover)

    p_stat = sub.add_parser("status", help="show what is in the database")
    p_stat.set_defaults(func=cmd_status)

    p_probe = sub.add_parser("probe", help="test an ATS board slug without saving it")
    p_probe.add_argument("--greenhouse", metavar="SLUG")
    p_probe.add_argument("--lever", metavar="SLUG")
    p_probe.add_argument("--ashby", metavar="SLUG")
    p_probe.set_defaults(func=cmd_probe)

    for name, phase in (("score", "2"), ("draft", "3"), ("serve", "4"), ("submit", "5")):
        p = sub.add_parser(name, help=f"Phase {phase} — not built yet")
        p.set_defaults(func=lambda a, n=name, ph=phase: _not_built(ph, n))

    args = parser.parse_args(argv)
    logging_setup.setup(verbose=args.verbose)

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
