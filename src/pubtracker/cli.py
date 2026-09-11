import argparse
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path

from .config import load_config
from .feed import render_feeds, write_feeds
from .http import HttpClient
from .matching import match_identity, name_candidate
from .models import Paper, normalize_doi
from .sources import arxiv, biorxiv, pubmed
from .state import ingest_batch, load_state, save_state

LOG = logging.getLogger(__name__)
ADAPTERS = {"biorxiv": biorxiv, "biorxiv-publications": biorxiv, "pubmed": pubmed, "arxiv": arxiv}


def fingerprint(researchers) -> str:
    raw = json.dumps([asdict(r) for r in researchers], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def update(config, state: dict, *, since: date | None = None, client=None, now=None) -> tuple[dict, dict]:
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    until = now.date()
    if since and since > until:
        raise ValueError("--since cannot be in the future")
    owned_client = client is None
    client = client or HttpClient(os.environ.get("NCBI_EMAIL") or config.update.get("ncbi_email", ""))
    all_papers, failures, counts = [], {}, {}
    identity_config = fingerprint(config.researchers)
    if state.get("identity_config") != identity_config:
        # Configuration edits take effect for already stored works as well.
        for work in state["works"].values():
            matches = {}
            for raw in work["records"].values():
                paper = Paper.from_dict(raw)
                for researcher in config.researchers:
                    evidence = match_identity(paper, researcher)
                    if evidence["matched"]:
                        matches.setdefault(researcher.id, []).append({"record": paper.key, **evidence})
            if matches != work["matched_researchers"]:
                work["matched_researchers"] = matches
                work["updated"] = stamp
        state["identity_config"] = identity_config
    try:
        for stream, adapter in ADAPTERS.items():
            source = "biorxiv" if stream == "biorxiv-publications" else stream
            researchers = [r for r in config.researchers if source in r.sources]
            if not researchers:
                continue
            signature = fingerprint(researchers)
            checkpoint = state["checkpoints"].get(stream, {})
            start = until - timedelta(days=config.update["initial_lookback_days"])
            if checkpoint.get("config") == signature:
                start = date.fromisoformat(checkpoint["last_success"][:10]) - timedelta(days=config.update["overlap_days"])
            start = since or start
            LOG.info("Fetching %s: %s through %s", stream, start, until)
            try:
                fetch = adapter.fetch_publications if stream == "biorxiv-publications" else adapter.fetch
                papers = fetch(client, researchers, start, until, config)
                tracked = {key: Paper.from_dict(raw) for w in state["works"].values()
                           for key, raw in w["records"].items() if raw["source"] == source}
                due = [p for key, p in tracked.items() if
                       (until - date.fromisoformat(state["refresh"].get(key, "1970-01-01")[:10])).days >= config.update["refresh_days"]]
                if stream == "biorxiv-publications":
                    due = []  # Refresh belongs to the independent details stream.
                if due:
                    LOG.info("Refreshing %s previously tracked %s records", len(due), source)
                    papers.extend(adapter.refresh(client, due, config) if source == "pubmed" else adapter.refresh(client, due))
                # Keep only researcher-name candidates or records with known strong identifiers.
                known_ids = {identifier for w in state["works"].values() for raw in w["records"].values()
                             for identifier in Paper.from_dict(raw).own_ids()}
                known_targets = {r["target"] for w in state["works"].values() for raw in w["records"].values() for r in raw["relations"]}
                papers = [p for p in papers if any(name_candidate(p, r) for r in researchers)
                          or p.own_ids() & (known_ids | known_targets)]
                all_papers.extend(papers)
                counts[stream] = len(papers)
                # Fetch + pagination + refresh must ALL succeed before advancement.
                state["checkpoints"][stream] = {"last_success": stamp, "config": signature}
                for p in due:
                    state["refresh"][p.key] = stamp
            except Exception as exc:
                # A source failure is isolated; the CLI still exits nonzero after saving successes.
                LOG.error("%s failed; checkpoint retained: %s", stream, exc)
                failures[stream] = f"{type(exc).__name__}: {exc}"
        summary = ingest_batch(state, all_papers, config.researchers, stamp)
        tracked_keys = {key for w in state["works"].values() for key in w["records"]}
        for paper in all_papers:
            if paper.key in tracked_keys:
                state["refresh"].setdefault(paper.key, stamp)
        # Rejected name matches are only a diagnostic sample; accepted works are never pruned.
        state["rejected"] = dict(sorted(state["rejected"].items(),
                                       key=lambda item: (item[1]["first_seen"], item[0]), reverse=True)[:500])
        summary.update({"fetched_candidates": counts, "works": len(state["works"]), "failures": failures})
        return state, summary
    finally:
        if owned_client:
            client.close()


def diagnose(state: dict, identifier: str) -> dict | None:
    original = identifier
    identifier = state["aliases"].get(identifier, identifier)
    doi = normalize_doi(identifier)
    if doi:
        identifier = f"doi:{doi}"
    for work in state["works"].values():
        if identifier == work["id"] or any(identifier in Paper.from_dict(p).own_ids() or
                                           identifier == p["source_id"] or
                                           identifier in {r["target"] for r in p["relations"]}
                                           for p in work["records"].values()):
            return {"requested_id": original, "result": "accepted", "work": work,
                    "retired_ids": [key for key, value in state["aliases"].items() if value == work["id"]]}
    for key, rejected in state["rejected"].items():
        paper = Paper.from_dict(rejected["record"])
        if identifier == key or identifier in paper.own_ids() or identifier == paper.source_id:
            return {"requested_id": original, "result": "rejected", **rejected}
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate conservative researcher Atom feeds")
    sub = parser.add_subparsers(dest="command", required=True)
    updating = sub.add_parser("update", help="Fetch, match, persist state and render feeds")
    updating.add_argument("--config", type=Path, default=Path("config/feeds.yaml"))
    updating.add_argument("--state", type=Path, default=Path("data/state.json"))
    updating.add_argument("--site-dir", type=Path, default=Path("site"))
    updating.add_argument("--since", type=date.fromisoformat, help="Override discovery start date (YYYY-MM-DD)")
    updating.add_argument("--dry-run", action="store_true", help="Fetch and render in memory; leave state/site unchanged")
    updating.add_argument("--output-dir", type=Path, help="With --dry-run, write preview feeds to a separate directory")
    diagnostic = sub.add_parser("diagnose", help="Explain a stored work or rejected candidate")
    diagnostic.add_argument("id", help="Work URN, DOI, PMID, arXiv ID or namespaced source ID")
    diagnostic.add_argument("--state", type=Path, default=Path("data/state.json"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        state = load_state(args.state)
        if args.command == "diagnose":
            result = diagnose(state, args.id)
            if result is None:
                print("Not found in stored works or the latest 500 rejected name candidates.")
                return 1
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if args.output_dir and (not args.dry_run or args.output_dir.resolve() == args.site_dir.resolve()):
            parser.error("--output-dir requires --dry-run and a directory different from --site-dir")
        config = load_config(args.config)
        state, summary = update(config, state, since=args.since)
        files = render_feeds(state, config)
        if args.dry_run:
            if args.output_dir:
                write_feeds(args.output_dir, files)
            summary["dry_run"] = True
            summary["preview_directory"] = str(args.output_dir) if args.output_dir else None
        else:
            # Render everything before touching disk. Save IDs before feeds so a retry is stable.
            save_state(args.state, state)
            write_feeds(args.site_dir, files)
        print(json.dumps(summary, indent=2))
        return 1 if summary["failures"] else 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        LOG.error("%s", exc)
        return 1
