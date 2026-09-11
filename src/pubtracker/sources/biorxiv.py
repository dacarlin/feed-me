from datetime import date, timedelta
import logging

from ..http import SourceError
from ..matching import name_candidate
from ..models import Author, Paper, Relation, compatible_names, normalize_doi, plain_text

BASE = "https://api.biorxiv.org"
LOG = logging.getLogger(__name__)


def parse_record(row: dict, *, publication: bool = False) -> Paper:
    prefix = "preprint_" if publication else ""
    doi = normalize_doi((row.get("preprint_doi") or row.get("biorxiv_doi")) if publication else row.get("doi"))
    if not doi:
        raise SourceError("bioRxiv record has no valid DOI")
    authors = [Author(plain_text(a)) for a in row.get(prefix + "authors", "").split(";") if a.strip()]
    corresponding = row.get(prefix + "author_corresponding", "")
    # The API only associates an institution with the corresponding author.
    candidates = [a for a in authors if compatible_names(a.name, corresponding)]
    if len(candidates) == 1:
        institution = plain_text(row.get(prefix + "author_corresponding_institution", ""))
        candidates[0].affiliations = [institution] if institution else []
    published = normalize_doi(row.get("published_doi") if publication else row.get("published"))
    relations = [Relation("is_preprint_of", f"doi:{published}", "bioRxiv published-version metadata")] if published and published != doi else []
    return Paper(source="biorxiv", source_id=doi, title=row.get(prefix + "title", ""),
                 authors=authors, abstract=plain_text(row.get(prefix + "abstract", "")),
                 date=row.get(prefix + "date", ""), url=f"https://doi.org/{doi}", doi=doi,
                 relations=relations, version=str(row.get("version", "")),
                 journal=row.get("published_journal", ""), publication_date=row.get("published_date", ""))


def pages(client, endpoint: str):
    cursor = 0
    previous = None
    while True:
        try:
            # The implicit-format details route can hang or return an empty 200.
            # Select the documented JSON route explicitly on every page.
            payload = client.get(f"{BASE}/{endpoint}/{cursor}/json").json()
        except SourceError as exc:
            raise SourceError(f"bioRxiv {endpoint} at cursor {cursor}: {exc}") from exc
        except ValueError as exc:
            raise SourceError(f"bioRxiv {endpoint} at cursor {cursor} returned empty or invalid JSON") from exc
        messages = payload.get("messages", [])
        if not messages:
            raise SourceError("bioRxiv response missing messages")
        message = messages[0]
        status = str(message.get("status", "")).lower()
        if status not in {"ok", "no posts found", "no papers found"}:
            raise SourceError(f"bioRxiv API: {message}")
        rows = payload.get("collection")
        if not isinstance(rows, list):
            raise SourceError("bioRxiv response missing collection")
        if status == "ok" and "total" not in message:
            raise SourceError("bioRxiv response missing pagination total")
        total = int(message.get("total", 0))
        if not rows:
            if cursor < total:
                raise SourceError("bioRxiv returned an incomplete page")
            return
        if rows == previous:
            raise SourceError("bioRxiv repeated a page")
        yield from rows
        cursor += len(rows)
        LOG.info("bioRxiv %s: %s/%s records fetched", endpoint, cursor, total)
        if cursor >= total:
            return
        previous = rows


def fetch(client, researchers, since: date, until: date, config) -> list[Paper]:
    papers = []
    for row in pages(client, f"details/biorxiv/{since}/{until}"):
        paper = parse_record(row)
        if any(name_candidate(paper, r) for r in researchers):
            papers.append(paper)
    return papers


def fetch_publications(client, researchers, since: date, until: date, config) -> list[Paper]:
    papers = []
    # Scan publication dates separately: preprints can be years older than this window.
    start = min(since, until - timedelta(days=config.update["publication_lookback_days"]))
    for row in pages(client, f"pubs/biorxiv/{start}/{until}"):
        paper = parse_record(row, publication=True)
        if any(name_candidate(paper, r) for r in researchers):
            papers.append(paper)
    return papers


def refresh(client, papers: list[Paper]) -> list[Paper]:
    results = []
    for paper in papers:
        payload = client.get(f"{BASE}/details/biorxiv/{paper.source_id}/na/json").json()
        rows = payload.get("collection", [])
        if not rows or any(str(m.get("status", "")).lower() != "ok" for m in payload.get("messages", [])):
            raise SourceError(f"bioRxiv refresh failed for {paper.key}")
        results.extend(parse_record(row) for row in rows)
    return results
