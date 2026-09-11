"""Readable state, immutable logical IDs, and atomic individual file writes."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from uuid import NAMESPACE_URL, uuid5

from .dedupe import find_matches
from .matching import match_identity
from .models import Paper, compatible_names


def empty_state() -> dict:
    return {"schema_version": 1, "checkpoints": {}, "refresh": {}, "works": {}, "aliases": {}, "rejected": {}}


def load_state(path: Path) -> dict:
    if not path.exists():
        return empty_state()
    state = json.loads(path.read_text())
    if state.get("schema_version") != 1:
        raise ValueError("Unsupported state schema; refusing to overwrite")
    for key in ("checkpoints", "refresh", "works", "aliases", "rejected"):
        if not isinstance(state.get(key), dict):
            raise ValueError(f"Invalid state: {key} must be an object")
    for key, work in state["works"].items():
        if work["id"] != key or not work["records"]:
            raise ValueError(f"Invalid work: {key}")
        for record in work["records"].values():
            Paper.from_dict(record)
    return state


def atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == content:
        return
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temp = Path(handle.name)
        handle.write(content)
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def save_state(path: Path, state: dict):
    atomic_write(path, json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def _append_unique(target: list, values: list):
    for value in values:
        if value not in target:
            target.append(value)


def combine_record(old: dict, paper: Paper) -> dict:
    new = paper.to_dict()
    # The pubs endpoint and older versions often contain less metadata.
    for field in ("abstract", "date", "journal", "version", "publication_date", "doi"):
        if not new[field]:
            new[field] = old[field]
    if old.get("version", "").isdigit() and (
        not paper.version or (paper.version.isdigit() and int(old["version"]) > int(paper.version))
    ):
        for field in ("title", "abstract", "authors", "date", "version"):
            new[field] = deepcopy(old[field])
    _append_unique(new["relations"], old["relations"])
    _append_unique(new["relations"], paper.to_dict()["relations"])
    _append_unique(new["identifiers"], old["identifiers"])
    for author in new["authors"]:
        previous = [a for a in old["authors"] if compatible_names(a["name"], author["name"])]
        if len(previous) == 1:
            _append_unique(author["affiliations"], previous[0]["affiliations"])
            if not author["orcid"]:
                author["orcid"] = previous[0]["orcid"]
    return new


def ingest(state: dict, paper: Paper, researchers: list, now: str) -> tuple[bool, dict]:
    decisions = {r.id: match_identity(paper, r) for r in researchers}
    direct = {rid: [{"record": paper.key, **d}] for rid, d in decisions.items() if d["matched"]}
    candidates = find_matches(paper, state["works"])
    strong = [(wid, m) for wid, m in candidates if m["rank"] <= 3]
    # A known DOI/cross-reference may carry identity; a similar title never can.
    inherited = {}
    for wid, match in strong:
        for rid in state["works"][wid]["matched_researchers"]:
            if any(r.id == rid and paper.source in r.sources for r in researchers):
                inherited.setdefault(rid, []).append({"record": paper.key, "matched": True,
                    "reason": "identity inherited through a strong work identifier",
                    "via_work": wid, "via_record": match["matched_record"], "method": match["method"]})
    if not direct and not inherited:
        return False, {"record": paper.to_dict(), "identity": decisions,
                       "deduplication": [{"work": w, **m} for w, m in candidates],
                       "reason": "no strong identity evidence or link to an identified work"}
    # Multiple heuristic candidates are ambiguous; do not join them transitively.
    selected = strong or (candidates if len(candidates) == 1 else [])
    if selected:
        winner = min((wid for wid, _ in selected), key=lambda wid: (state["works"][wid]["first_seen"], wid))
        work = state["works"][winner]
        before = deepcopy(work)
        for loser, _ in selected:
            if loser == winner:
                continue
            other = state["works"].pop(loser)
            work["records"].update(other["records"])
            for rid, evidence in other["matched_researchers"].items():
                _append_unique(work["matched_researchers"].setdefault(rid, []), evidence)
            _append_unique(work["deduplication"], other["deduplication"])
            state["aliases"][loser] = winner
            for alias, destination in list(state["aliases"].items()):
                if destination == loser:
                    state["aliases"][alias] = winner
            _append_unique(work["deduplication"], [{"record": paper.key, "method": "merge_existing_works",
                                                  "retained_id": winner, "retired_id": loser}])
        events = [{"record": paper.key, **m} for _, m in selected]
    else:
        winner = "urn:uuid:" + str(uuid5(NAMESPACE_URL, "pubtracker:" + paper.key))
        work = {"id": winner, "first_seen": now, "updated": now, "records": {},
                "matched_researchers": {}, "deduplication": []}
        state["works"][winner] = work
        before = deepcopy(work)
        events = [{"record": paper.key, "method": "new_work",
                   "reason": "ambiguous title candidates" if len(candidates) > 1 else "no existing work"}]
    old = work["records"].get(paper.key)
    work["records"][paper.key] = combine_record(old, paper) if old else paper.to_dict()
    for rid in set(direct) | set(inherited):
        # Direct evidence is more useful and avoids accumulating self-inheritance.
        evidence = direct.get(rid) or inherited[rid]
        existing = work["matched_researchers"].setdefault(rid, [])
        if direct.get(rid) or not any(e["record"] == paper.key for e in existing):
            _append_unique(existing, evidence)
    if not old or old != work["records"][paper.key] or len(selected) > 1:
        _append_unique(work["deduplication"], events)
    if work != before:
        work["updated"] = now
    state["rejected"].pop(paper.key, None)
    return True, {"work": winner}


def ingest_batch(state: dict, papers: list[Paper], researchers: list, now: str) -> dict:
    pending = sorted(papers, key=lambda p: (p.source, p.source_id, int(p.version) if p.version.isdigit() else 0))
    accepted = set()
    while pending:
        remaining = []
        diagnostics = {}
        for paper in pending:
            ok, diagnostic = ingest(state, paper, researchers, now)
            if ok:
                accepted.add(paper.key)
            else:
                remaining.append(paper)
                diagnostics[paper.key] = diagnostic
        if len(remaining) == len(pending):
            # Keep a bounded diagnostic sample of name candidates, not the global corpus.
            for key, diagnostic in diagnostics.items():
                previous = state["rejected"].get(key, {})
                state["rejected"][key] = {"first_seen": previous.get("first_seen", now), **diagnostic}
            break
        pending = remaining
    return {"accepted_records": len(accepted), "rejected_records": len({p.key for p in pending})}
