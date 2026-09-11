"""Small JSON-friendly records; source metadata stays attached to its author."""

from dataclasses import asdict, dataclass, field
from html import unescape
import re
import unicodedata
from urllib.parse import unquote


def plain_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return " ".join(unescape(value).split())


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", plain_text(value)).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", value).split())


def normalize_doi(value: str | None) -> str | None:
    value = unquote((value or "").strip()).lower()
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", value)
    # Preserve meaningful DOI suffix punctuation, including balanced parentheses.
    value = value.strip().rstrip(".,;")
    return value if re.fullmatch(r"10\.\d{4,9}/[^\s<>]+", value) else None


def normalize_orcid(value: str | None) -> str | None:
    value = re.sub(r"^https?://orcid\.org/", "", (value or "").strip(), flags=re.I)
    value = value.upper()
    if not re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", value):
        return None
    total = 0
    for digit in value.replace("-", "")[:-1]:
        total = (total + int(digit)) * 2
    check = (12 - total % 11) % 11
    return value if value[-1] == ("X" if check == 10 else str(check)) else None


def name_parts(name: str) -> tuple[str, list[str]]:
    if "," in name:
        family, given = name.split(",", 1)
        return normalize_title(family), normalize_title(given).split()
    parts = normalize_title(name).split()
    return (parts[-1], parts[:-1]) if parts else ("", [])


def compatible_names(left: str, right: str) -> bool:
    lf, lg = name_parts(left)
    rf, rg = name_parts(right)
    if not lf or lf != rf or not lg or not rg:
        return False
    # Extra middle names are deliberately not inferred from a name without them.
    if len(lg) != len(rg):
        return False
    return all(a == b or (a[0] == b[0] and (len(a) == 1 or len(b) == 1))
               for a, b in zip(lg, rg))


@dataclass
class Author:
    name: str
    affiliations: list[str] = field(default_factory=list)
    orcid: str | None = None


@dataclass
class Relation:
    kind: str
    target: str  # Namespaced identifier: doi:..., pubmed:..., arxiv:...
    evidence: str


@dataclass
class Paper:
    source: str
    source_id: str
    title: str
    authors: list[Author]
    abstract: str = ""
    date: str = ""  # ISO date; partial dates (YYYY or YYYY-MM) are preserved.
    url: str = ""
    doi: str | None = None
    status: str = "preprint"
    journal: str = ""
    version: str = ""
    publication_date: str = ""
    identifiers: list[str] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)

    def __post_init__(self):
        self.doi = normalize_doi(self.doi)
        self.title = plain_text(self.title)
        if not self.source_id or not self.title:
            raise ValueError(f"Incomplete {self.source} record: missing identifier or title")

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"

    def own_ids(self) -> set[str]:
        return {self.key, *self.identifiers} | ({f"doi:{self.doi}"} if self.doi else set())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "Paper":
        value = dict(value)
        value["authors"] = [Author(**a) for a in value.get("authors", [])]
        value["relations"] = [Relation(**r) for r in value.get("relations", [])]
        return cls(**value)
