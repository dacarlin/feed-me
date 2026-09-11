from copy import deepcopy
from datetime import date
import json

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
        return Response(next(self.responses))


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
    client = Client([raw])
    papers = arxiv.fetch(client, config.researchers, date(2026, 9, 9), date(2026, 9, 10), config)
    assert len(papers) == 1
    assert papers[0].date == "2020-01-01"
    assert client.calls[0][1]["sortBy"] == "lastUpdatedDate"
