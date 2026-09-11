"""Ordered, explainable links between records. Titles never establish identity."""

from difflib import SequenceMatcher
import re

from .models import Paper, compatible_names, normalize_title


def _targets(paper: Paper, other: Paper) -> dict[str, str]:
    targets = {}
    for relation in paper.relations:
        if relation.kind in {"is_preprint_of", "has_preprint", "same_as"} or (
            relation.kind == "update_of" and other.status == "preprint"
            and relation.target in other.own_ids()
        ):
            targets[relation.target] = relation.evidence
    return targets


def compare(left: Paper, right: Paper) -> dict | None:
    lt, rt = _targets(left, right), _targets(right, left)
    related = (set(lt) & right.own_ids()) | (set(rt) & left.own_ids()) | (set(lt) & set(rt))
    if related:
        evidence = sorted({t[k] for t in (lt, rt) for k in related if k in t})
        bio = all("bioRxiv" in e for e in evidence)
        return {"rank": 2 if bio else 1, "method": "biorxiv_published_version" if bio else "explicit_relationship",
                "identifiers": sorted(related), "evidence": evidence}
    shared = left.own_ids() & right.own_ids()
    if shared:
        return {"rank": 3, "method": "shared_identifier", "identifiers": sorted(shared)}

    # Separate journal papers and separate submissions to one server stay separate.
    if left.source == right.source or (left.status == right.status == "published"):
        return None
    a, b = normalize_title(left.title), normalize_title(right.title)
    if min(len(a.split()), len(b.split())) < 6 or min(len(a), len(b)) < 40:
        return None
    if not left.date[:4].isdigit() or not right.date[:4].isdigit():
        return None
    years = abs(int(left.date[:4]) - int(right.date[:4]))
    if years > 5:
        return None
    # One-to-one author overlap; repeated/group names cannot inflate the count.
    available = list({x.name for x in right.authors})
    overlap = []
    for author in sorted({x.name for x in left.authors}):
        match = next((n for n in available if compatible_names(author, n)), None)
        if match:
            overlap.append(author)
            available.remove(match)
    denominator = min(len({x.name for x in left.authors}), len({x.name for x in right.authors}))
    fraction = len(overlap) / max(1, denominator)
    if a == b and len(overlap) >= 2 and fraction >= 0.5:
        return {"rank": 4, "method": "normalized_title_and_authors", "overlapping_authors": overlap}
    score = SequenceMatcher(None, a, b, autojunk=False).ratio()
    tokens_a, tokens_b = set(a.split()), set(b.split())
    jaccard = len(tokens_a & tokens_b) / max(1, len(tokens_a | tokens_b))
    first_author = left.authors and right.authors and compatible_names(left.authors[0].name, right.authors[0].name)
    # Small number/negation changes can reverse a scientific claim.
    critical = lambda title: re.findall(r"\d+|\b(?:not|no|without|with)\b", title)
    if (score >= 0.97 and jaccard >= 0.85 and len(overlap) >= 3 and fraction >= 0.8
            and first_author and years <= 2 and critical(a) == critical(b)):
        return {"rank": 5, "method": "conservative_fuzzy_title_and_authors",
                "similarity": round(score, 4), "overlapping_authors": overlap}
    return None


def find_matches(paper: Paper, works: dict) -> list[tuple[str, dict]]:
    matches = []
    for work_id, work in works.items():
        candidates = []
        for key, raw in work["records"].items():
            match = compare(paper, Paper.from_dict(raw))
            if match:
                candidates.append({**match, "matched_record": key})
        if candidates:
            matches.append((work_id, min(candidates, key=lambda m: (m["rank"], m["matched_record"]))))
    return sorted(matches, key=lambda m: (m[1]["rank"], m[0]))
