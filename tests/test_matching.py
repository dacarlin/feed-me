from copy import deepcopy

import pytest

from pubtracker.matching import match_identity
from pubtracker.models import Author, normalize_doi, normalize_title
from pubtracker.sources import arxiv, pubmed
from conftest import FIXTURES


def test_baker_affiliations(preprint, publication, researcher):
    assert match_identity(preprint, researcher)["matched"]
    evidence = match_identity(publication, researcher)
    assert evidence["matched"]
    assert evidence["evidence"][0]["author"] == "David Baker"


def test_unrelated_baker_and_coauthor_affiliations(researcher):
    unrelated = pubmed.parse_xml((FIXTURES / "pubmed.xml").read_bytes())[1]
    assert not match_identity(unrelated, researcher)["matched"]


@pytest.mark.parametrize("name,affiliations", [
    ("David Baker", []),
    ("David Baker", ["Department of Biochemistry, University of Oxford"]),
    ("David Baker", ["University of Washington"]),
    ("David Baker", ["Department of Biochemistry, Washington University in St. Louis"]),
    ("Daniel Baker", ["Institute for Protein Design, University of Washington"]),
    ("David J Baker", ["Institute for Protein Design, University of Washington"]),
])
def test_weak_or_conflicting_identity_rejected(preprint, researcher, name, affiliations):
    preprint.authors = [Author(name, affiliations)]
    assert not match_identity(preprint, researcher)["matched"]


def test_initials_require_affiliations(preprint, researcher):
    preprint.authors[-1].name = "Baker, D."
    assert match_identity(preprint, researcher)["matched"]
    preprint.authors[-1].affiliations = []
    assert not match_identity(preprint, researcher)["matched"]


def test_orcid_match_and_conflict(preprint, researcher):
    researcher = deepcopy(researcher)
    # Valid example ORCID used only in synthetic tests; not assigned to David Baker.
    researcher.orcid = "0000-0002-1825-0097"
    author = preprint.authors[-1]
    author.orcid = "https://orcid.org/0000-0002-1825-0097"
    author.affiliations = []
    assert match_identity(preprint, researcher)["matched"]
    author.affiliations = ["Institute for Protein Design, University of Washington"]
    author.orcid = "0000-0001-5109-3700"
    assert not match_identity(preprint, researcher)["matched"]


def test_arxiv_missing_affiliation_is_rejected(researcher):
    papers, _, _ = arxiv.parse_xml((FIXTURES / "arxiv.xml").read_bytes())
    assert not match_identity(papers[0], researcher)["matched"]
    assert match_identity(papers[1], researcher)["matched"]


@pytest.mark.parametrize("raw,expected", [
    (" HTTPS://DOI.ORG/10.1234/ABC ", "10.1234/abc"),
    ("doi: 10.1234/ABC.", "10.1234/abc"),
    ("http://dx.doi.org/10.1234%2FABC", "10.1234/abc"),
    ("10.1234/a(b)", "10.1234/a(b)"),
    ("NA", None), (None, None), ("10.12/no", None), ("10.1234/with space", None),
])
def test_doi_normalization(raw, expected):
    assert normalize_doi(raw) == expected


def test_title_normalization():
    assert normalize_title("  De novo <i>Protein</i>–Design &amp; Assembly! ") == "de novo protein design assembly"
    assert normalize_title("Protein 2 design") != normalize_title("Protein 3 design")
