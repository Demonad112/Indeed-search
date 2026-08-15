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


def cmd_score(args: argparse.Namespace) -> int:
    from jobpipe import score

    return score.run(
        limit=args.limit,
        job_id=args.job_id,
        rescore=args.rescore,
        calibrate=args.calibrate,
        dry_run=args.dry_run,
    )


def cmd_draft(args: argparse.Namespace) -> int:
    from jobpipe import draft

    return draft.run(
        threshold=args.threshold,
        limit=args.limit,
        job_id=args.job_id,
        regenerate=args.regenerate,
        feedback=args.feedback,
        dry_run=args.dry_run,
    )


def cmd_serve(args: argparse.Namespace) -> int:
    from jobpipe import review

    return review.run(host=args.host, port=args.port)


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

    p_score = sub.add_parser("score", help="Phase 2 — score postings against criteria.yaml")
    p_score.add_argument("--calibrate", action="store_true",
                         help="score the three reference postings and check them against their anchors")
    p_score.add_argument("--limit", type=int, help="score at most N postings")
    p_score.add_argument("--job-id", help="score a single job by id")
    p_score.add_argument("--rescore", action="store_true",
                         help="also re-score postings that already have a score")
    p_score.add_argument("--dry-run", action="store_true",
                         help="print the exact request without calling the API")
    p_score.set_defaults(func=cmd_score)

    p_draft = sub.add_parser("draft", help="Phase 3 — write tailored resumes and cover letters")
    p_draft.add_argument("--threshold", type=int, help="score floor (default: criteria.yaml)")
    p_draft.add_argument("--limit", type=int, help="draft at most N postings")
    p_draft.add_argument("--job-id", help="draft a single job by id")
    p_draft.add_argument("--regenerate", action="store_true", help="redo drafts that already exist")
    p_draft.add_argument("--feedback", help="what to change when regenerating")
    p_draft.add_argument("--dry-run", action="store_true", help="list what would be drafted")
    p_draft.set_defaults(func=cmd_draft)

    p_serve = sub.add_parser("serve", help="Phase 4 — open the private review dashboard")
    p_serve.add_argument("--host", default="127.0.0.1",
                         help="bind address (default 127.0.0.1 — anything else is exposed)")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.set_defaults(func=cmd_serve)

    for name, phase in (("submit", "5"),):
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
