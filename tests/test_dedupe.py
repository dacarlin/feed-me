from copy import deepcopy

from pubtracker.dedupe import compare
from pubtracker.models import Author, Relation
from pubtracker.state import empty_state, ingest_batch

T1 = "2026-09-01T10:00:00Z"
T2 = "2026-09-10T10:00:00Z"


def test_title_and_authors(preprint, publication):
    assert compare(preprint, publication)["rank"] == 4
    publication.authors = [Author("Someone Else"), Author("David Baker")]
    assert compare(preprint, publication) is None


def test_shared_doi_overrides_title(preprint, publication):
    publication.doi = preprint.doi
    publication.title = "Completely changed title"
    assert compare(preprint, publication)["method"] == "shared_identifier"


def test_explicit_relation_overrides_title(preprint, publication):
    preprint.relations = [Relation("is_preprint_of", f"doi:{publication.doi}", "bioRxiv published-version metadata")]
    publication.title = "A totally new title"
    assert compare(preprint, publication)["rank"] == 2


def test_no_merge_on_weak_title_or_different_numbers(preprint, publication):
    publication.title = "De novo design of moderately stable protein assemblies"
    assert compare(preprint, publication) is None
    preprint.title += " using method 12345"
    publication.title = preprint.title.replace("12345", "12346")
    assert compare(preprint, publication) is None


def test_fuzzy_requires_strong_coauthors(preprint, publication):
    preprint.title += " through guided diffusion of backbone structures"
    publication.title = preprint.title + "s"
    assert compare(preprint, publication)["rank"] == 5
    publication.authors = publication.authors[:1]
    assert compare(preprint, publication) is None


def test_two_journal_articles_not_merged(publication):
    other = deepcopy(publication)
    other.source = "other"
    other.source_id = "different"
    other.identifiers = []
    other.doi = "10.1234/different"
    assert compare(publication, other) is None


def test_preprint_to_publication_preserves_id(preprint, publication, researcher):
    state = empty_state()
    ingest_batch(state, [preprint], [researcher], T1)
    wid = next(iter(state["works"]))
    ingest_batch(state, [publication], [researcher], T2)
    assert list(state["works"]) == [wid]
    assert state["works"][wid]["first_seen"] == T1
    assert len(state["works"][wid]["records"]) == 2


def test_publication_before_preprint_is_stable(preprint, publication, researcher):
    state = empty_state()
    ingest_batch(state, [publication], [researcher], T1)
    wid = next(iter(state["works"]))
    ingest_batch(state, [preprint], [researcher], T2)
    assert list(state["works"]) == [wid]


def test_bridge_merges_existing_works_retains_oldest(preprint, publication, researcher):
    publication.title = "Entirely different publication title"
    state = empty_state()
    ingest_batch(state, [preprint], [researcher], T1)
    oldest = next(iter(state["works"]))
    ingest_batch(state, [publication], [researcher], T2)
    assert len(state["works"]) == 2
    preprint.relations.append(Relation("is_preprint_of", f"doi:{publication.doi}", "bioRxiv published-version metadata"))
    ingest_batch(state, [preprint], [researcher], T2)
    assert list(state["works"]) == [oldest]
    assert list(state["aliases"].values()) == [oldest]


def test_unknown_identity_cannot_be_bootstrapped_by_title(preprint, publication, researcher):
    state = empty_state()
    ingest_batch(state, [publication], [researcher], T1)
    for author in preprint.authors:
        author.affiliations = []
    ingest_batch(state, [preprint], [researcher], T2)
    assert preprint.key in state["rejected"]
    preprint.relations.append(Relation("is_preprint_of", f"doi:{publication.doi}", "bioRxiv published-version metadata"))
    ingest_batch(state, [preprint], [researcher], T2)
    assert preprint.key not in state["rejected"]
    assert len(next(iter(state["works"].values()))["records"]) == 2


def test_generic_pubmed_updateof_is_not_a_preprint_relation(publication):
    other = deepcopy(publication)
    other.source_id = "new-pmid"
    other.doi = "10.1234/correction"
    other.identifiers = []
    other.relations.append(Relation("update_of", publication.key, "PubMed UpdateOf"))
    assert compare(other, publication) is None
    publication.status = "preprint"
    assert compare(other, publication)["rank"] == 1


def test_older_version_does_not_overwrite_latest(preprint, researcher):
    older = deepcopy(preprint)
    preprint.version = "2"
    preprint.title = "New revised title for this protein design paper"
    state = empty_state()
    ingest_batch(state, [preprint], [researcher], T1)
    ingest_batch(state, [older], [researcher], T2)
    work = next(iter(state["works"].values()))
    assert work["records"][preprint.key]["title"] == preprint.title
    older.version = ""  # The publications endpoint may describe an old title.
    older.publication_date = "2026-09-09"
    ingest_batch(state, [older], [researcher], T2)
    assert work["records"][preprint.key]["title"] == preprint.title
    assert work["records"][preprint.key]["publication_date"] == "2026-09-09"
