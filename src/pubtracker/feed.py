"""Deterministic Atom 1.0; reader identity comes from the persisted work ID."""

from html import escape
from pathlib import Path
from xml.etree import ElementTree as ET

from .models import Paper
from .state import atomic_write

ATOM = "http://www.w3.org/2005/Atom"
ET.register_namespace("", ATOM)


def node(parent, tag, text=None, **attrs):
    element = ET.SubElement(parent, f"{{{ATOM}}}{tag}", attrs)
    if text is not None:
        element.text = text
    return element


def preferred_record(work: dict) -> Paper:
    records = [Paper.from_dict(p) for p in work["records"].values()]
    return max(records, key=lambda p: (p.status == "published", bool(p.abstract), p.date, p.key))


def publication_urls(work: dict) -> list[str]:
    urls = set()
    for raw in work["records"].values():
        paper = Paper.from_dict(raw)
        if paper.status == "published":
            urls.add(paper.url)
        for relation in paper.relations:
            if relation.kind == "is_preprint_of":
                if relation.target.startswith("doi:"):
                    urls.add("https://doi.org/" + relation.target[4:])
                elif relation.target.startswith("pubmed:"):
                    urls.add("https://pubmed.ncbi.nlm.nih.gov/" + relation.target[7:] + "/")
    return sorted(urls)


def content_html(work: dict, preferred: Paper, published: list[str]) -> str:
    records = [Paper.from_dict(p) for p in work["records"].values()]
    status = "Published" if published else "Preprint"
    parts = [f"<p><strong>{status}</strong></p>",
             "<p>Authors: " + escape("; ".join(a.name for a in preferred.authors)) + "</p>",
             "<p>Sources: " + escape(", ".join(sorted({p.source for p in records}))) + "</p>"]
    if preferred.date:
        parts.append(f"<p>{'Publication' if preferred.status == 'published' else 'Preprint'} date: {escape(preferred.date)}</p>")
    publication_dates = sorted({p.publication_date for p in records if p.publication_date})
    if publication_dates:
        parts.append("<p>Linked publication date: " + escape(", ".join(publication_dates)) + "</p>")
    if preferred.journal:
        parts.append(f"<p>Journal: {escape(preferred.journal)}</p>")
    abstract = preferred.abstract or next((p.abstract for p in records if p.abstract), "")
    if preferred.status == "preprint" and published:
        parts.append("<p>A published version is linked below. Title, authors and abstract are from the preprint.</p>")
    if abstract:
        parts.extend(f"<p>{escape(p)}</p>" for p in abstract.split("\n\n"))
    parts.append("<ul>")
    for paper in sorted(records, key=lambda p: p.key):
        label = f"{paper.source} ({paper.status})" + (f" — DOI: {paper.doi}" if paper.doi else "")
        parts.append(f'<li><a href="{escape(paper.url, quote=True)}">{escape(label)}</a></li>')
    for url in published:
        label = "Published version" + (" — DOI: " + url.removeprefix("https://doi.org/") if url.startswith("https://doi.org/") else "")
        parts.append(f'<li><a href="{escape(url, quote=True)}">{escape(label)}</a></li>')
    parts.append("</ul>")
    return "\n".join(parts)


def render_feeds(state: dict, config) -> dict[str, str]:
    files = {}
    base = config.site["base_url"]
    for spec in config.feeds:
        root = ET.Element(f"{{{ATOM}}}feed")
        url = f"{base}/{spec['id']}.xml"
        node(root, "id", url)
        node(root, "title", spec.get("title", spec["id"]))
        node(root, "link", href=url, rel="self", type="application/atom+xml")
        node(root, "link", href=base + "/", rel="alternate")
        node(node(root, "author"), "name", "pubtracker")
        works = [w for w in state["works"].values() if set(spec["researchers"]) & set(w["matched_researchers"])]
        # Backfills and metadata corrections can discover old papers today.
        # Keep recent publications ahead of those when applying the feed limit.
        works.sort(key=lambda w: (preferred_record(w).date, w["first_seen"], w["id"]), reverse=True)
        works = works[:config.site["max_entries"]]
        node(root, "updated", max((w["updated"] for w in works), default="1970-01-01T00:00:00Z"))
        for work in works:
            paper = preferred_record(work)
            published = publication_urls(work)
            canonical = paper.url if paper.status == "published" else next(iter(published), paper.url)
            entry = node(root, "entry")
            node(entry, "id", work["id"])
            node(entry, "title", paper.title)
            # Discovery time is stable, even if the paper's bibliographic date changes.
            node(entry, "published", work["first_seen"])
            node(entry, "updated", work["updated"])
            node(entry, "link", href=canonical, rel="alternate")
            for raw in sorted(work["records"].values(), key=lambda p: (p["source"], p["source_id"])):
                if raw["url"] != canonical:
                    node(entry, "link", href=raw["url"], rel="related", title=raw["source"])
            node(entry, "category", term="published" if published else "preprint")
            for author in paper.authors:
                node(node(entry, "author"), "name", author.name)
            node(entry, "content", content_html(work, paper, published), type="html")
        ET.indent(root, space="  ")
        files[f"{spec['id']}.xml"] = '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
    links = "\n".join(f'<li><a href="{escape(f["id"])}.xml">{escape(f.get("title", f["id"]))}</a></li>' for f in config.feeds)
    files["index.html"] = ('<!doctype html>\n<html lang="en"><meta charset="utf-8">'
                           '<meta name="viewport" content="width=device-width">'
                           f'<title>{escape(config.site["title"])}</title>\n'
                           f'<h1>{escape(config.site["title"])}</h1>\n'
                           '<p>Copy a feed link into NetNewsWire to subscribe.</p>\n'
                           f'<ul>\n{links}\n</ul>\n</html>\n')
    files[".nojekyll"] = ""
    return files


def write_feeds(directory: Path, files: dict[str, str]):
    for filename, content in files.items():
        atomic_write(directory / filename, content)
