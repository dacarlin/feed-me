from datetime import date
import calendar
import os
import re

from defusedxml import ElementTree as ET

from ..http import SourceError
from ..models import Author, Paper, Relation, name_parts, normalize_doi, normalize_orcid

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def text(node, path: str = ".") -> str:
    found = node.find(path)
    return " ".join("".join(found.itertext()).split()) if found is not None else ""


def article_date(node) -> str:
    if node is None:
        return ""
    year = text(node, "Year")
    if not year:
        match = re.search(r"\b(\d{4})\b", text(node, "MedlineDate"))
        return match[1] if match else ""
    month = text(node, "Month")
    if not month:
        return year
    if not month.isdigit():
        months = {name.lower(): i for i, name in enumerate(calendar.month_abbr) if name}
        month = str(months.get(month[:3].lower(), ""))
    if not month:
        return year
    result = f"{year}-{int(month):02}"
    day = text(node, "Day")
    return result + f"-{int(day):02}" if day.isdigit() else result


def parse_xml(content: bytes | str) -> list[Paper]:
    root = ET.fromstring(content)
    if root.tag != "PubmedArticleSet" or root.find(".//ERROR") is not None:
        raise SourceError("Invalid PubMed EFetch response")
    results = []
    for node in root.findall("PubmedArticle"):
        citation = node.find("MedlineCitation")
        article = citation.find("Article")
        pmid = text(citation, "PMID")
        authors = []
        for author in article.findall("AuthorList/Author"):
            name = " ".join(filter(None, [text(author, "ForeName") or text(author, "Initials"), text(author, "LastName")]))
            orcid = next((normalize_orcid(text(i)) for i in author.findall("Identifier")
                          if i.get("Source", "").lower() == "orcid"), None)
            authors.append(Author(name or text(author, "CollectiveName"),
                                  [text(a) for a in author.findall("AffiliationInfo/Affiliation")], orcid))
        # Scope IDs to the article itself, never its bibliography or related citations.
        ids = {i.get("IdType", "").lower(): text(i) for i in node.findall("PubmedData/ArticleIdList/ArticleId")}
        doi = normalize_doi(ids.get("doi")) or next((normalize_doi(text(i)) for i in article.findall("ELocationID")
                                                    if i.get("EIdType") == "doi"), None)
        journal = text(article, "Journal/Title")
        types = {text(t).lower() for t in article.findall("PublicationTypeList/PublicationType")}
        preprint = "preprint" in types or "biorxiv" in journal.lower() or "medrxiv" in journal.lower()
        relations = []
        for correction in citation.findall("CommentsCorrectionsList/CommentsCorrections"):
            target = text(correction, "PMID")
            kind = correction.get("RefType")
            if target and kind == "UpdateIn" and preprint:
                relations.append(Relation("is_preprint_of", f"pubmed:{target}", "PubMed preprint UpdateIn"))
            elif target and kind == "UpdateOf" and not preprint:
                relations.append(Relation("update_of", f"pubmed:{target}", "PubMed UpdateOf; target must be a preprint"))
        abstract = "\n\n".join((a.get("Label", "") + ": " if a.get("Label") else "") + text(a)
                                 for a in article.findall("Abstract/AbstractText"))
        date_node = article.find("ArticleDate")
        if date_node is None:
            date_node = article.find("Journal/JournalIssue/PubDate")
        results.append(Paper(source="pubmed", source_id=pmid, title=text(article, "ArticleTitle"),
                             authors=authors, abstract=abstract, date=article_date(date_node),
                             url=f"https://doi.org/{doi}" if doi else f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                             doi=doi, status="preprint" if preprint else "published", journal=journal,
                             identifiers=[f"pmc:{ids['pmc'].upper()}"] if ids.get("pmc") else [], relations=relations))
    return results


def parameters(config=None) -> dict:
    params = {"db": "pubmed", "tool": "pubtracker"}
    email = os.environ.get("NCBI_EMAIL") or (config.update.get("ncbi_email") if config else "")
    if email:
        params["email"] = email
    return params


def fetch_ids(client, ids: list[str], config=None) -> list[Paper]:
    results = []
    for offset in range(0, len(ids), 200):
        batch = ids[offset:offset + 200]
        response = client.get(f"{BASE}/efetch.fcgi", {**parameters(config), "id": ",".join(batch), "retmode": "xml"})
        papers = parse_xml(response.content)
        if {p.source_id for p in papers} != set(batch):
            raise SourceError("PubMed EFetch omitted requested IDs; checkpoint not advanced")
        results.extend(papers)
    return results


def fetch(client, researchers, since: date, until: date, config) -> list[Paper]:
    names = []
    for r in researchers:
        family, given = name_parts(r.name)
        names.append(f'"{family} {given[0][0]}"[au]')
        if r.orcid:
            names.append(f'"{r.orcid}"[auid]')
    # Publication dates also find papers indexed ahead of their release date.
    # Keep creation/revision dates to discover late indexing and corrections.
    dates = " OR ".join(f'("{since}"[{field}] : "{until}"[{field}])'
                        for field in ("pdat", "crdt", "lr"))
    term = f"({' OR '.join(names)}) AND ({dates})"
    ids = []
    while True:
        payload = client.get(f"{BASE}/esearch.fcgi", {**parameters(config), "term": term,
                              "retmode": "json", "retmax": 200, "retstart": len(ids)}).json()
        result = payload.get("esearchresult", {})
        if payload.get("error") or result.get("errorlist") or "count" not in result:
            raise SourceError(f"PubMed ESearch failed: {payload}")
        count = int(result["count"])
        if count > 9999:
            raise SourceError("PubMed query exceeds 9,999 results; use smaller --since windows")
        page = result.get("idlist", [])
        if count > len(ids) and (not page or set(ids) & set(page)):
            raise SourceError("PubMed returned an incomplete/repeated page")
        ids.extend(page)
        if len(ids) >= count:
            return fetch_ids(client, ids, config)


def refresh(client, papers: list[Paper], config=None) -> list[Paper]:
    return fetch_ids(client, [p.source_id for p in papers], config)
