from copy import deepcopy
from dataclasses import replace
from datetime import date
import json
from xml.etree import ElementTree as ET

import pytest

from pubtracker.http import SourceError
from pubtracker.matching import match_identity
from pubtracker.sources import arxiv, biorxiv, pubmed
from conftest import FIXTURES


class Response:
    def __init__(self, data):
        self.data = data
        self.content = data.encode() if isinstance(data, str) else data

    def json(self):
        return self.data


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return Response(result)


def test_biorxiv_publication_metadata():
    raw = json.loads((FIXTURES / "biorxiv_pubs.json").read_text())["collection"][0]
    paper = biorxiv.parse_record(raw, publication=True)
    assert paper.relations[0].target == "doi:10.1234/proteins.2026.42"
    assert paper.status == "preprint"
    assert paper.publication_date == "2026-09-09"
    raw["biorxiv_doi"] = raw.pop("preprint_doi")
    assert biorxiv.parse_record(raw, publication=True).doi == paper.doi


def test_biorxiv_does_not_assign_coauthor_institution(researcher):
    raw = json.loads((FIXTURES / "biorxiv.json").read_text())["collection"][0]
    raw["author_corresponding"] = "Alice Chen"
    assert not match_identity(biorxiv.parse_record(raw), researcher)["matched"]


def test_biorxiv_pagination_and_empty_page_failure():
    first = json.loads((FIXTURES / "biorxiv.json").read_text())
    first["messages"][0]["total"] = 2
    second = deepcopy(first)
    second["collection"][0]["doi"] = "10.1101/2026.09.02.123456"
    client = Client([first, second])
    assert len(list(biorxiv.pages(client, "details/biorxiv/2026-09-01/2026-09-10"))) == 2
    assert client.calls[0][0].endswith("/0/json")
    assert client.calls[1][0].endswith("/1/json")
    second["collection"] = []
    with pytest.raises(SourceError, match="incomplete"):
        list(biorxiv.pages(Client([first, second]), "details/biorxiv/test"))


def test_biorxiv_no_posts_is_success():
    client = Client([{"messages": [{"status": "no posts found", "total": 0}], "collection": []}])
    assert list(biorxiv.pages(client, "details/biorxiv/test")) == []


def test_biorxiv_publications_use_explicit_json(config):
    client = Client([json.loads((FIXTURES / "biorxiv_pubs.json").read_text())])
    assert biorxiv.fetch_publications(client, config.researchers, date(2026, 9, 1), date(2026, 9, 10), config)
    assert client.calls[0][0] == "https://api.biorxiv.org/pubs/biorxiv/2026-08-11/2026-09-10/0/json"


@pytest.mark.parametrize("publication", [False, True])
def test_biorxiv_filters_unrelated_broken_records_before_parsing(config, publication):
    filename = "biorxiv_pubs.json" if publication else "biorxiv.json"
    prefix = "preprint_" if publication else ""
    doi_field = "preprint_doi" if publication else "doi"
    first = json.loads((FIXTURES / filename).read_text())
    good = deepcopy(first["collection"][0])
    broken = {**good, doi_field: "", prefix + "title": "", prefix + "authors": "Example, E."}
    first["collection"] = [good, broken]
    first["messages"][0]["total"] = 3
    second = deepcopy(first)
    second["collection"] = [{**good, doi_field: "10.1101/2026.09.03.123456"}]
    client = Client([first, second])
    fetch = biorxiv.fetch_publications if publication else biorxiv.fetch
    papers = fetch(client, config.researchers, date(2026, 9, 1), date(2026, 9, 10), config)
    assert len(papers) == 2
    assert papers[1].doi == "10.1101/2026.09.03.123456"
    assert client.calls[1][0].endswith("/2/json")


@pytest.mark.parametrize("publication", [False, True])
def test_biorxiv_still_validates_matching_candidates(config, publication):
    filename = "biorxiv_pubs.json" if publication else "biorxiv.json"
    row = json.loads((FIXTURES / filename).read_text())["collection"][0]
    row["preprint_doi" if publication else "doi"] = ""
    with pytest.raises(SourceError, match="no valid DOI"):
        biorxiv.matching_records([row], config.researchers, publication=publication)


@pytest.mark.parametrize("authors", [None, "", [], 123])
def test_biorxiv_does_not_discard_records_with_unknown_authors(config, authors):
    with pytest.raises(SourceError, match="cannot determine relevance"):
        biorxiv.matching_records([{"authors": authors}], config.researchers)


def test_pubmed_parser_preserves_scoped_metadata():
    papers = pubmed.parse_xml((FIXTURES / "pubmed.xml").read_bytes())
    assert papers[0].doi == "10.1234/proteins.2026.42"
    assert papers[0].date == "2026-09-09"
    assert papers[1].date == "2026"
    assert "BACKGROUND:" in papers[0].abstract
    assert "&" in papers[0].abstract
    assert papers[0].relations == []  # Neither references nor errata imply the same work.
    assert len(papers[0].authors[-1].affiliations) == 2


def test_pubmed_preprint_updatein():
    raw = (FIXTURES / "pubmed.xml").read_text().replace("Journal Article", "Preprint").replace("ErratumFor", "UpdateIn")
    paper = pubmed.parse_xml(raw)[0]
    assert paper.status == "preprint"
    assert paper.relations[0].target == "pubmed:88888888"


def test_pubmed_search_uses_publication_creation_and_revision_dates(config):
    client = Client([{"esearchresult": {"count": "0", "idlist": []}}])
    assert pubmed.fetch(client, config.researchers, date(2026, 9, 1), date(2026, 9, 10), config) == []
    params = client.calls[0][1]
    for field in ("pdat", "crdt", "lr"):
        assert f'("2026-09-01"[{field}] : "2026-09-10"[{field}])' in params["term"]
    assert params["tool"] == "pubtracker"


def test_pubmed_does_not_silently_truncate(config):
    client = Client([{"esearchresult": {"count": "10000", "idlist": []}}])
    with pytest.raises(SourceError, match="9,999"):
        pubmed.fetch(client, config.researchers, date(2026, 9, 1), date(2026, 9, 10), config)
    with pytest.raises(SourceError, match="omitted"):
        pubmed.fetch_ids(Client(["<PubmedArticleSet />"]), ["123"], config)


def test_arxiv_ids_and_publication_relationship():
    papers, total, dates = arxiv.parse_xml((FIXTURES / "arxiv.xml").read_bytes())
    assert total == 2 and dates[0] == "2026-09-09"
    assert papers[0].source_id == "2609.12345"
    assert papers[0].version == "2"
    assert papers[0].doi is None
    assert papers[0].relations[0].target == "doi:10.1234/proteins.2026.42"


def test_arxiv_errors_are_failures():
    raw = (FIXTURES / "arxiv.xml").read_text().replace("http://arxiv.org/abs/2609.12345v2", "http://arxiv.org/api/errors#incorrect_id_format")
    with pytest.raises(SourceError, match="arXiv API"):
        arxiv.parse_xml(raw)


def test_arxiv_discovers_revised_old_submissions(config):
    raw = (FIXTURES / "arxiv.xml").read_text().replace("<published>2026-09-01", "<published>2020-01-01")
    client = Client([raw, raw])
    papers = arxiv.fetch(client, config.researchers, date(2026, 9, 9), date(2026, 9, 10), config)
    assert len(papers) == 1
    assert papers[0].date == "2020-01-01"
    assert client.calls[0][1]["sortBy"] == "lastUpdatedDate"


def arxiv_page(ids, total, updated="2026-09-09"):
    root = ET.fromstring((FIXTURES / "arxiv.xml").read_text())
    template = root.find("a:entry", arxiv.NS)
    for entry in root.findall("a:entry", arxiv.NS):
        root.remove(entry)
    root.find("os:totalResults", arxiv.NS).text = str(total)
    for identifier in ids:
        entry = deepcopy(template)
        entry.find("a:id", arxiv.NS).text = f"http://arxiv.org/abs/{identifier}v1"
        entry.find("a:updated", arxiv.NS).text = updated + "T12:00:00Z"
        root.append(entry)
    return ET.tostring(root)


def test_arxiv_pages_each_author_and_deduplicates_shared_papers(config):
    client = Client([
        arxiv_page(["2609.00001", "2609.00002"], 50).replace(b"00002v1", b"00002v2"),
        arxiv_page(["2609.00003"], 50, updated="2026-09-01"),
        arxiv_page(["2609.00002", "2609.00004"], 2),
    ])
    papers = arxiv.fetch(client, config.researchers, date(2026, 9, 9), date(2026, 9, 10), config)
    assert {p.source_id for p in papers} == {"2609.00001", "2609.00002", "2609.00004"}
    assert len(papers) == 3
    assert next(p for p in papers if p.source_id == "2609.00002").version == "2"
    assert [(p["search_query"], p["start"]) for _, p in client.calls] == [
        ('au:"baker"', 0), ('au:"baker"', 2), ('au:"herschlag"', 0),
    ]
    assert all(p["max_results"] == 25 for _, p in client.calls)


def test_arxiv_queries_each_surname_once_and_continues_after_empty_author(config):
    researchers = [*config.researchers, replace(config.researchers[0], name="Dana Baker")]
    client = Client([arxiv_page([], 0), arxiv_page(["2609.00001"], 1)])
    papers = arxiv.fetch(client, researchers, date(2026, 9, 1), date(2026, 9, 10), config)
    assert len(client.calls) == 2 and len(papers) == 1


@pytest.mark.parametrize("second", [arxiv_page([], 3), arxiv_page(["2609.00001"], 3)])
def test_arxiv_rejects_incomplete_or_repeated_author_pages(config, second):
    client = Client([arxiv_page(["2609.00001"], 3), second])
    with pytest.raises(SourceError, match="author 'baker'.*incomplete/repeated"):
        arxiv.fetch(client, config.researchers, date(2026, 9, 1), date(2026, 9, 10), config)


def test_arxiv_refresh_uses_small_batches_and_requires_all_ids():
    ids = [f"2609.{i:05}" for i in range(26)]
    papers = arxiv.parse_xml(arxiv_page(ids, 26))[0]
    client = Client([arxiv_page(ids[:25], 25), arxiv_page(ids[25:], 1)])
    assert len(arxiv.refresh(client, papers)) == 26
    assert [p["id_list"].split(",") for _, p in client.calls] == [ids[:25], ids[25:]]
    with pytest.raises(SourceError, match="refresh omitted"):
        arxiv.refresh(Client([arxiv_page(ids[:24], 24)]), papers)
