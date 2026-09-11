"""Identity requires an author-scoped ORCID or multiple affiliation clues."""

from .config import Researcher
from .models import Paper, compatible_names, normalize_orcid, normalize_title


def name_candidate(paper: Paper, researcher: Researcher) -> bool:
    return any(compatible_names(a.name, researcher.name) or
               (researcher.orcid and normalize_orcid(a.orcid) == researcher.orcid)
               for a in paper.authors)


def match_identity(paper: Paper, researcher: Researcher) -> dict:
    evidence = []
    if paper.source not in researcher.sources:
        return {"matched": False, "reason": "source disabled", "evidence": []}
    for author in paper.authors:
        orcid = normalize_orcid(author.orcid)
        if researcher.orcid and orcid == researcher.orcid:
            evidence.append({"author": author.name, "method": "orcid", "orcid": orcid})
            continue
        if not compatible_names(author.name, researcher.name):
            continue
        if researcher.orcid and orcid and orcid != researcher.orcid:
            evidence.append({"author": author.name, "method": "rejected",
                             "reason": "conflicting ORCID", "orcid": orcid})
            continue
        text = " " + normalize_title(" ".join(author.affiliations)) + " "
        hits = [a for a in researcher.affiliations if " " + normalize_title(a) + " " in text]
        accepted = (len(hits) >= researcher.min_affiliation_matches
                    and set(researcher.required_affiliations) <= set(hits))
        evidence.append({"author": author.name,
                         "method": "name_and_affiliations" if accepted else "rejected",
                         "affiliations": author.affiliations, "matched_affiliations": hits,
                         **({} if accepted else {"reason": "insufficient author-scoped affiliation evidence"})})
    matched = any(e["method"] != "rejected" for e in evidence)
    return {"matched": matched, "reason": "strong author identity" if matched else "identity not established",
            "evidence": evidence}
