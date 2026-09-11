from dataclasses import dataclass, field
from pathlib import Path
import re
from urllib.parse import urlparse

import yaml

from .models import normalize_orcid

SOURCES = {"biorxiv", "pubmed", "arxiv"}


@dataclass
class Researcher:
    id: str
    name: str
    affiliations: list[str]
    sources: list[str]
    required_affiliations: list[str] = field(default_factory=list)
    min_affiliation_matches: int = 2
    orcid: str | None = None


@dataclass
class Config:
    researchers: list[Researcher]
    feeds: list[dict]
    site: dict
    update: dict


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text())
    researchers = [Researcher(**r) for r in raw["researchers"]]
    feeds = raw["feeds"]
    for entries, label in ((researchers, "researcher"), (feeds, "feed")):
        ids = [r.id if isinstance(r, Researcher) else r["id"] for r in entries]
        if len(ids) != len(set(ids)) or any(not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", x) for x in ids):
            raise ValueError(f"{label} IDs must be unique lowercase slugs")
    known = {r.id for r in researchers}
    for r in researchers:
        if not r.name.strip() or not r.sources or not set(r.sources) <= SOURCES:
            raise ValueError(f"Invalid name or sources for {r.id}")
        if r.orcid:
            r.orcid = normalize_orcid(r.orcid)
            if not r.orcid:
                raise ValueError(f"Invalid ORCID for {r.id}")
        if r.min_affiliation_matches < 1:
            raise ValueError("min_affiliation_matches must be positive")
        if not set(r.required_affiliations) <= set(r.affiliations):
            raise ValueError("required_affiliations must also appear in affiliations")
        if len(set(r.affiliations)) != len(r.affiliations):
            raise ValueError("Affiliation clues must be distinct")
    for f in feeds:
        if not f["researchers"] or not set(f["researchers"]) <= known:
            raise ValueError(f"Unknown or empty researcher list in feed {f['id']}")
    site = {"title": "Research publications", "max_entries": 200, **raw.get("site", {})}
    base = urlparse(site.get("base_url", ""))
    if base.scheme not in {"http", "https"} or not base.netloc or base.query or base.fragment:
        raise ValueError("site.base_url must be an absolute HTTP(S) URL without query/fragment")
    site["base_url"] = site["base_url"].rstrip("/")
    update = {"initial_lookback_days": 7, "overlap_days": 2,
              "publication_lookback_days": 30, "refresh_days": 7, "ncbi_email": "",
              **raw.get("update", {})}
    for key in ("initial_lookback_days", "overlap_days", "publication_lookback_days", "refresh_days"):
        if not isinstance(update[key], int) or update[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    if not isinstance(site["max_entries"], int) or site["max_entries"] < 1:
        raise ValueError("max_entries must be positive")
    return Config(researchers, feeds, site, update)
