import json
from pathlib import Path

import pytest

from pubtracker.config import load_config
from pubtracker.sources import biorxiv, pubmed

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parents[1]


@pytest.fixture
def config():
    return load_config(ROOT / "config/feeds.yaml")


@pytest.fixture
def researcher(config):
    return config.researchers[0]


@pytest.fixture
def preprint():
    return biorxiv.parse_record(json.loads((FIXTURES / "biorxiv.json").read_text())["collection"][0])


@pytest.fixture
def publication():
    return pubmed.parse_xml((FIXTURES / "pubmed.xml").read_bytes())[0]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Normal tests must not use the network")
    monkeypatch.setattr("requests.Session.request", fail)
