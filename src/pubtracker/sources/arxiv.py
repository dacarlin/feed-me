from datetime import date
import logging
import re

from defusedxml import ElementTree as ET

from ..http import SourceError
from ..models import Author, Paper, Relation, name_parts, normalize_doi, plain_text

BASE = "https://export.arxiv.org/api/query"
PAGE_SIZE = 25
LOG = logging.getLogger(__name__)
NS = {"a": "http://www.w3.org/2005/Atom", "ar": "http://arxiv.org/schemas/atom",
      "os": "http://a9.com/-/spec/opensearch/1.1/"}


def parse_xml(content: bytes | str) -> tuple[list[Paper], int, list[str]]:
    root = ET.fromstring(content)
    if root.tag != f"{{{NS['a']}}}feed":
        raise SourceError("Invalid arXiv Atom response")
    papers, updated = [], []
    for entry in root.findall("a:entry", NS):
        url = entry.findtext("a:id", "", NS)
        if "/api/errors" in url:
            raise SourceError(f"arXiv API: {entry.findtext('a:summary', '', NS)}")
        source_id = re.sub(r"v\d+$", "", url.split("/abs/")[-1])
        if "/abs/" not in url or not source_id:
            raise SourceError("arXiv record missing ID")
        version = re.search(r"v(\d+)$", url)
        authors = [Author(plain_text(a.findtext("a:name", "", NS)),
                          [plain_text(x.text or "") for x in a.findall("ar:affiliation", NS)])
                   for a in entry.findall("a:author", NS)]
        doi = normalize_doi(entry.findtext("ar:doi", "", NS))
        # arxiv:doi points to the journal version, not to the arXiv preprint.
        relations = [Relation("is_preprint_of", f"doi:{doi}", "arXiv journal DOI")] if doi else []
        papers.append(Paper(source="arxiv", source_id=source_id, title=entry.findtext("a:title", "", NS),
                            authors=authors, abstract=plain_text(entry.findtext("a:summary", "", NS)),
                            date=entry.findtext("a:published", "", NS)[:10], url=f"https://arxiv.org/abs/{source_id}",
                            relations=relations, journal=entry.findtext("ar:journal_ref", "", NS),
                            version=version[1] if version else ""))
        stamp = entry.findtext("a:updated", "", NS)[:10]
        date.fromisoformat(stamp)  # A malformed page must not advance the checkpoint.
        updated.append(stamp)
    total = root.findtext("os:totalResults", None, NS)
    if total is None:
        raise SourceError("arXiv response missing totalResults")
    return papers, int(total), updated


def fetch(client, researchers, since: date, until: date, config) -> list[Paper]:
    # Stable, small queries reduce server work and can reuse arXiv's GET cache
    # when another researcher is configured. Keep surname-only discovery so
    # abbreviated given names and revisions of old submissions remain visible.
    names = sorted({name_parts(r.name)[0] for r in researchers})
    results = {}
    for name in names:
        try:
            for paper in fetch_author(client, name, since, until):
                previous = results.get(paper.key)
                if previous is None or int(paper.version or 0) >= int(previous.version or 0):
                    results[paper.key] = paper
        except SourceError as exc:
            raise SourceError(f"arXiv author {name!r}: {exc}") from exc
    return list(results.values())


def fetch_author(client, name: str, since: date, until: date) -> list[Paper]:
    start, results, seen = 0, [], set()
    while True:
        response = client.get(BASE, {"search_query": f'au:"{name}"', "sortBy": "lastUpdatedDate",
                                    "sortOrder": "descending", "start": start, "max_results": PAGE_SIZE})
        papers, total, updates = parse_xml(response.content)
        if (start < total and not papers) or any(p.key in seen for p in papers):
            raise SourceError("arXiv returned an incomplete/repeated page")
        results.extend(p for p, stamp in zip(papers, updates) if str(since) <= stamp <= str(until))
        seen.update(p.key for p in papers)
        start += len(papers)
        LOG.info("arXiv author %r: %s/%s records fetched", name, start, total)
        if start >= total or (updates and min(updates) < str(since)):
            return results
        if start >= 30000:
            raise SourceError("arXiv result limit reached; narrow researcher configuration")


def refresh(client, papers: list[Paper]) -> list[Paper]:
    results = []
    for offset in range(0, len(papers), PAGE_SIZE):
        batch = papers[offset:offset + PAGE_SIZE]
        response = client.get(BASE, {"id_list": ",".join(p.source_id for p in batch), "max_results": PAGE_SIZE})
        parsed, _, _ = parse_xml(response.content)
        if {p.key for p in parsed} != {p.key for p in batch}:
            raise SourceError("arXiv refresh omitted requested IDs")
        results.extend(parsed)
    return results
