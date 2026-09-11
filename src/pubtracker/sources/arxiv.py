from datetime import date
import re

from defusedxml import ElementTree as ET

from ..http import SourceError
from ..models import Author, Paper, Relation, name_parts, normalize_doi, plain_text

BASE = "https://export.arxiv.org/api/query"
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
    # Sort by update time so revisions of old submissions are also discovered.
    names = sorted({name_parts(r.name)[0] for r in researchers})
    query = " OR ".join(f'au:"{name}"' for name in names)
    start, results, seen = 0, [], set()
    while True:
        response = client.get(BASE, {"search_query": query, "sortBy": "lastUpdatedDate",
                                    "sortOrder": "descending", "start": start, "max_results": 100})
        papers, total, updates = parse_xml(response.content)
        if (start < total and not papers) or any(p.key in seen for p in papers):
            raise SourceError("arXiv returned an incomplete/repeated page")
        results.extend(p for p, stamp in zip(papers, updates) if str(since) <= stamp <= str(until))
        seen.update(p.key for p in papers)
        start += len(papers)
        if start >= total or (updates and min(updates) < str(since)):
            return results
        if start >= 30000:
            raise SourceError("arXiv result limit reached; narrow researcher configuration")


def refresh(client, papers: list[Paper]) -> list[Paper]:
    results = []
    for offset in range(0, len(papers), 100):
        batch = papers[offset:offset + 100]
        response = client.get(BASE, {"id_list": ",".join(p.source_id for p in batch), "max_results": 100})
        parsed, _, _ = parse_xml(response.content)
        if {p.key for p in parsed} != {p.key for p in batch}:
            raise SourceError("arXiv refresh omitted requested IDs")
        results.extend(parsed)
    return results
