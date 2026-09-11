from copy import deepcopy
from datetime import date, datetime, timezone
import json
from types import SimpleNamespace

import feedparser
import pytest

from pubtracker.cli import diagnose, main, update
from pubtracker.feed import render_feeds
from pubtracker.http import SourceError
from pubtracker.models import Relation
from pubtracker.sources import arxiv
from pubtracker.state import empty_state, ingest_batch, load_state, save_state
from conftest import FIXTURES, ROOT
from test_dedupe import T1, T2

NOW = datetime(2026, 9, 10, 10, tzinfo=timezone.utc)


def adapters(monkeypatch, preprint, publication):
    def fail(*args):
        raise SourceError("fixture outage")
    monkeypatch.setattr("pubtracker.cli.ADAPTERS", {
        "biorxiv": SimpleNamespace(fetch=lambda *a: [deepcopy(preprint)], refresh=lambda *a: []),
        "pubmed": SimpleNamespace(fetch=lambda *a: [deepcopy(publication)], refresh=lambda *a: []),
        "arxiv": SimpleNamespace(fetch=fail),
    })


def test_stable_valid_feed_and_unchanged_rerender(config, preprint, publication):
    state = empty_state()
    ingest_batch(state, [preprint], config.researchers, T1)
    first = feedparser.parse(render_feeds(state, config)["all.xml"])
    assert not first.bozo
    assert len(first.entries) == 1
    ingest_batch(state, [publication], config.researchers, T2)
    files = render_feeds(state, config)
    second = feedparser.parse(files["all.xml"])
    assert not second.bozo
    assert first.entries[0].id == second.entries[0].id
    assert first.entries[0].published == second.entries[0].published
    assert second.entries[0].link == publication.url
    assert "BACKGROUND:" in second.entries[0].content[0].value
    assert "biorxiv" in second.entries[0].content[0].value
    assert "10.1234/proteins.2026.42" in second.entries[0].content[0].value
    ingest_batch(state, [preprint, publication], config.researchers, "2026-09-11T10:00:00Z")
    assert render_feeds(state, config) == files


def test_link_only_publication_status_and_preprint_abstract(config, preprint, publication):
    preprint.relations.append(Relation("is_preprint_of", f"doi:{publication.doi}", "bioRxiv published-version metadata"))
    state = empty_state()
    ingest_batch(state, [preprint], config.researchers, T1)
    entry = feedparser.parse(render_feeds(state, config)["all.xml"]).entries[0]
    assert entry.link == publication.url
    assert entry.tags[0].term == "published"
    assert "from the preprint" in entry.content[0].value


def test_same_batch_identifier_inheritance_is_order_independent(config, publication):
    unconfirmed = arxiv.parse_xml((FIXTURES / "arxiv.xml").read_bytes())[0][0]
    publication.title = "A completely changed journal publication title"
    state = empty_state()
    ingest_batch(state, [unconfirmed, publication], config.researchers, T1)
    assert len(state["works"]) == 1
    work = next(iter(state["works"].values()))
    assert len(work["records"]) == 2
    assert any(e.get("via_record") == publication.key for e in work["matched_researchers"]["david-baker"])


def test_feed_escapes_html(config, preprint):
    preprint.abstract = '<script>alert("x")</script> & text'
    state = empty_state()
    ingest_batch(state, [preprint], config.researchers, T1)
    xml = render_feeds(state, config)["all.xml"]
    assert "&amp;lt;script&amp;gt;" in xml


def test_feeds_select_researcher_union(config, preprint):
    state = empty_state()
    ingest_batch(state, [preprint], config.researchers, T1)
    config.feeds.append({"id": "other", "researchers": ["someone-else"]})
    assert len(feedparser.parse(render_feeds(state, config)["all.xml"]).entries) == 1
    assert not feedparser.parse(render_feeds(state, config)["other.xml"]).entries


def test_source_failure_retains_checkpoint_and_other_sources_progress(monkeypatch, config, preprint, publication):
    adapters(monkeypatch, preprint, publication)
    state = empty_state()
    checkpoint = {"last_success": T1, "config": "old"}
    state["checkpoints"]["arxiv"] = checkpoint.copy()
    state, summary = update(config, state, client=object(), now=NOW)
    assert state["checkpoints"]["arxiv"] == checkpoint
    assert state["checkpoints"]["pubmed"]["last_success"] == T2
    assert len(state["works"]) == 1
    assert "arxiv" in summary["failures"]


def test_failure_after_partial_page_discards_source_batch(monkeypatch, config):
    def fail(*args):
        raise SourceError("incomplete page")
    monkeypatch.setattr("pubtracker.cli.ADAPTERS", {"biorxiv": SimpleNamespace(fetch=fail)})
    state, summary = update(config, empty_state(), client=object(), now=NOW)
    assert not state["checkpoints"] and not state["works"]
    assert summary["failures"]


def test_biorxiv_publication_stream_survives_details_outage(monkeypatch, config, preprint, publication):
    def fail(*args):
        raise SourceError("empty details response")
    preprint.relations.append(Relation("is_preprint_of", f"doi:{publication.doi}", "bioRxiv published-version metadata"))
    adapter = SimpleNamespace(fetch=fail, fetch_publications=lambda *args: [preprint])
    monkeypatch.setattr("pubtracker.cli.ADAPTERS", {"biorxiv": adapter, "biorxiv-publications": adapter})
    state, summary = update(config, empty_state(), client=object(), now=NOW)
    assert "biorxiv" not in state["checkpoints"]
    assert "biorxiv-publications" in state["checkpoints"]
    assert len(state["works"]) == 1
    assert set(summary["failures"]) == {"biorxiv"}


def test_conflicting_config_removes_old_identity(monkeypatch, config, preprint):
    state = empty_state()
    ingest_batch(state, [preprint], config.researchers, T1)
    config.researchers[0].required_affiliations = ["University of Oxford"]
    monkeypatch.setattr("pubtracker.cli.ADAPTERS", {})
    state, _ = update(config, state, client=object(), now=NOW)
    assert next(iter(state["works"].values()))["matched_researchers"] == {}


def test_checkpoint_lookback_and_new_researcher_rescan(monkeypatch, config):
    starts = []
    def fetch(client, researchers, since, until, cfg):
        starts.append(since)
        return []
    monkeypatch.setattr("pubtracker.cli.ADAPTERS", {"pubmed": SimpleNamespace(fetch=fetch)})
    state, _ = update(config, empty_state(), client=object(), now=NOW)
    assert starts[-1] == date(2026, 9, 3)
    update(config, state, client=object(), now=NOW)
    assert starts[-1] == date(2026, 9, 8)
    config.researchers[0].name = "Another Researcher"
    update(config, state, client=object(), now=NOW)
    assert starts[-1] == date(2026, 9, 3)


def test_weekly_refresh_finds_old_publication(monkeypatch, config, preprint, publication):
    state = empty_state()
    ingest_batch(state, [preprint], config.researchers, T1)
    preprint.relations.append(Relation("is_preprint_of", f"doi:{publication.doi}", "bioRxiv published-version metadata"))
    refreshed = []
    def refresh(client, papers):
        refreshed.extend(papers)
        return [preprint]
    monkeypatch.setattr("pubtracker.cli.ADAPTERS", {"biorxiv": SimpleNamespace(fetch=lambda *a: [], refresh=refresh)})
    state, _ = update(config, state, client=object(), now=NOW)
    assert len(refreshed) == 1
    assert next(iter(state["works"].values()))["records"][preprint.key]["relations"]
    update(config, state, client=object(), now=NOW)
    assert len(refreshed) == 1


def test_dry_run_writes_only_explicit_preview(monkeypatch, tmp_path, config, preprint, publication, capsys):
    adapters(monkeypatch, preprint, publication)
    state_path, site = tmp_path / "state.json", tmp_path / "site"
    save_state(state_path, empty_state())
    site.mkdir()
    (site / "all.xml").write_text("sentinel")
    before = state_path.read_bytes()
    preview = tmp_path / "preview"
    result = main(["update", "--dry-run", "--state", str(state_path), "--site-dir", str(site),
                   "--config", str(ROOT / "config/feeds.yaml"), "--output-dir", str(preview)])
    assert result == 1  # Fixture arXiv failure remains visible despite successful preview.
    assert state_path.read_bytes() == before
    assert (site / "all.xml").read_text() == "sentinel"
    feed = feedparser.parse((preview / "all.xml").read_bytes())
    assert not feed.bozo and len(feed.entries) == 1
    assert json.loads(capsys.readouterr().out)["dry_run"]


def test_state_roundtrip_diagnose_and_schema_guard(tmp_path, config, preprint):
    state = empty_state()
    ingest_batch(state, [preprint], config.researchers, T1)
    path = tmp_path / "state.json"
    save_state(path, state)
    assert load_state(path) == state
    diagnostic = diagnose(state, "https://doi.org/" + preprint.doi)
    assert diagnostic["result"] == "accepted"
    assert diagnostic["work"]["matched_researchers"]["david-baker"][0]["evidence"]
    path.write_text('{"schema_version": 999}')
    with pytest.raises(ValueError, match="schema"):
        load_state(path)
