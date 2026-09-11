# Publication feeds

A small Python tracker for selected researchers. It fetches official bioRxiv,
PubMed and arXiv metadata, conservatively identifies authors, groups versions of
the same work, and writes static **Atom 1.0** feeds for NetNewsWire. GitHub Actions
runs every three hours; GitHub Pages serves `site/`. YAML is configuration and
committed, indented JSON is the persistent state.

## Run locally

Requires Python 3.11 or newer. Run these commands from the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest
python -m pubtracker update --dry-run
```

A dry run performs real, rate-limited API requests and renders feeds in memory.
It does **not** change `data/state.json` or `site/`. To inspect its output:

```bash
python -m pubtracker update --dry-run --output-dir /tmp/pubtracker-preview
open /tmp/pubtracker-preview/index.html
```

Write actual state and feeds with:

```bash
python -m pubtracker update
```

Commit `data/state.json` and `site/` together after a local update. The included
state and feeds contain live works backfilled from 2026-08-12 through 2026-09-11
UTC; future updates extend this state. All normal tests use synthetic
fixtures and explicitly prohibit network requests.

The default initial discovery window is 30 days, with two days of overlap on
subsequent updates. bioRxiv publication links have a separate 30-day window.
Tracked source records are refreshed every seven days to catch late metadata and
publication links. All date windows use UTC. PubMed searches publication,
creation **or revision** dates, covering both newly released papers indexed in
advance and older papers newly indexed or corrected. Feeds select and order
works by bibliographic date so old indexing changes cannot displace recent
papers at the entry limit. arXiv discovery uses descending update dates, including revisions to
old submissions.

To start further back, override the discovery window:

```bash
python -m pubtracker update --since 2026-08-01 --dry-run
python -m pubtracker update --since 2026-08-01
```

Large bioRxiv backfills scan all posts in the interval and can take a while. The
PubMed adapter fails explicitly above 9,999 search hits; use a more recent start
date or a narrower researcher configuration. This is a tracker for a small
personal reading list, not a bulk bibliography downloader.

## Configure researchers and feeds

Edit [`config/feeds.yaml`](config/feeds.yaml). Researchers specify identity rules
and enabled sources. Feeds specify unions of researcher IDs. For example:

```yaml
researchers:
  - id: david-baker
    name: David Baker
    orcid: 0000-0001-7896-6217
    affiliations:
      - Institute for Protein Design
      - University of Washington
      - Department of Biochemistry
    required_affiliations:
      - University of Washington
    min_affiliation_matches: 2
    sources: [biorxiv, pubmed, arxiv]

feeds:
  - id: all
    researchers: [david-baker]
  - id: david-baker
    researchers: [david-baker]
```

Add another researcher to `researchers`, then add their ID to any feed's
`researchers` list. Optional feed `title` controls its display name. IDs must be
unique lowercase slugs. `site.max_entries` defaults to 200 per feed; older works
remain in JSON. Keep feed IDs and `site.base_url` stable after subscribing.
Remove obsolete XML files from `site/` yourself when deleting or renaming a feed.

Changing a researcher's identity/source configuration triggers a fresh initial
discovery window for affected sources and re-evaluates stored identity evidence.
Use `--since` when a newly added researcher needs a longer backfill.

### David Baker identity policy

The configured ORCID, **0000-0001-7896-6217**, is explicitly associated with David
Baker and the University of Washington on [PNNL's EMSL profile](https://www.emsl.pnnl.gov/people/david-baker).
It is also listed for David Baker on page 20 of a [Baker Lab-hosted eLife paper](https://www.bakerlab.org/wp-content/uploads/2018/06/elife-28909-v2-1.pdf).

An author matches through an exact, checksum-valid configured ORCID, or through
a compatible name **and that author's own affiliations**. For the initial
configuration, affiliation matching requires University of Washington plus IPD
or Department of Biochemistry. A conflicting valid ORCID rejects that author's
name/affiliation match. Initials such as `Baker, D.` require the same affiliation
evidence. Unconfigured middle names and generic name-only matches are rejected.

A coauthor's UW affiliation never establishes Baker's identity. bioRxiv's API
only supplies the corresponding author's institution; it is attached only when
that corresponding name identifies exactly one listed author. No PDF, HTML
article or JATS scraping is used. arXiv often omits author affiliations, so its
name matches frequently remain unconfirmed. These intentional false negatives
are preferable to mixing in unrelated David Bakers.

A record without sufficient affiliation data may join an already identified
work through a shared DOI, source ID or explicit version relationship. Title
similarity does not establish a researcher's identity.

## Deduplication and stable entries

The ordered matching rules in [`dedupe.py`](src/pubtracker/dedupe.py) are:

1. Explicit version relationships, such as arXiv's journal DOI or PubMed's
   preprint `UpdateIn` link. PubMed `UpdateOf` is accepted only when the target
   record is a preprint; ordinary corrections, citations and errata are excluded.
2. bioRxiv published-version metadata from both details and publication endpoints.
3. Identical normalized DOIs, source IDs or other article-scoped identifiers.
4. Identical normalized titles with at least two overlapping authors and at
   least 50% of the smaller author list. Titles must have six words and 40
   characters, and dates must be within five years.
5. Fuzzy titles only at similarity ≥0.97 and token overlap ≥0.85, with at least
   three overlapping authors, ≥80% author overlap, a matching first author and
   dates within two years. Numbers and basic negations must agree.

Title rules do not merge two different journal articles or distinct submissions
within the same source. Multiple plausible title candidates remain separate.
Source records and the rule used for each merge remain inspectable in state.

Each work receives a persisted `urn:uuid:…` Atom entry ID. A later journal
publication changes its metadata, canonical link, status and `updated` time,
while retaining its ID and persisted `published` time. The Atom `published`
timestamp starts at the first known bibliographic date, rather than discovery
time, so [NetNewsWire's date sorter](https://github.com/Ranchero-Software/NetNewsWire/blob/main/Shared/Timeline/ArticleSorter.swift)
places old indexing corrections behind recent papers.
Year/month-only dates use the first day of that period; missing or invalid dates
fall back to discovery time. Existing state gains this timestamp on loading;
IDs and `first_seen` are preserved. Bibliographic dates also appear in the
content. If only a publication link is known, the entry labels its title
and abstract as preprint metadata.

If two previously emitted works are later linked, the earliest first-seen work's
ID survives and the other becomes an alias. A reader may retain the already
downloaded duplicate; a static feed cannot erase an item from NetNewsWire's
local history. Reader-specific treatment of updated content is also outside the
tracker's control. Subscribing to both `all` and `david-baker` initially gives you
two subscriptions containing the same works; choose one.

## State and diagnostics

[`data/state.json`](data/state.json) contains:

| Field | Purpose |
| --- | --- |
| `schema_version` | Reject unsupported formats instead of overwriting them |
| `checkpoints` | Last successful time and configuration fingerprint for each source stream |
| `refresh` | Last scheduled refresh time for tracked source records |
| `works` | Stable IDs, first-seen/updated timestamps, source records, identifiers, DOI relationships, matched researchers and evidence, deduplication decisions |
| `aliases` | Retired logical IDs mapped to their surviving work |
| `rejected` | Up to 500 rejected name candidates, including identity and deduplication explanations |
| `identity_config` | Fingerprint used to re-evaluate identity after YAML changes |

bioRxiv discovery and publication links have independent checkpoints because
one endpoint can work while the other is unavailable. Failures retain the
affected checkpoint and discard that stream's incomplete batch. Other streams
still update and produce feeds. The CLI exits **1** if any stream fails, even
when it successfully writes partial updates. The workflow deploys those updates
and then reports the source failure visibly. Empty/invalid responses and
incomplete pagination are failures, not successful empty result sets.

Examples (substitute an ID that actually appears in your state):

```bash
python -m pubtracker diagnose '10.1038/s41586-026-10464-0'
python -m pubtracker diagnose 'pubmed:12345678'
python -m pubtracker diagnose 'arxiv:2609.12345'
python -m pubtracker diagnose 'urn:uuid:YOUR-STORED-WORK-UUID'
```

`diagnose` reads local state only. It prints the source records, author evidence,
merge decisions and retired IDs, or explains a stored rejection. Records never
retrieved, lacking even a compatible author name, or evicted from the rejected
sample cannot be diagnosed. Dry-run results are not added to committed state.

For repairs, back up `data/state.json`, edit it, inspect the diff, then run a dry
update with preview output. Preserve each work's `id` and `first_seen` whenever
possible. To undo a bad merge, move the incorrect records into a separate work
with its own UUID and review `matched_researchers`, `deduplication` and aliases.
Fix the underlying identity/relationship evidence too, or a later fetch may
recreate the merge. Historical non-empty metadata and relationships are retained
when a later endpoint omits them; explicit removal currently requires repair.
Use one updater at a time locally. Writes are atomic per file, with state saved
before feeds; a failed feed write can be retried using the saved IDs.

## Enable GitHub Actions and GitHub Pages

1. Push this project, including `data/state.json`, `site/` and both workflow files,
   to the repository's **default branch**. For this checkout the remote is
   `dacarlin/feed-me`. GitHub Free supports Pages for public repositories; private
   repositories require a plan that supports Pages.
2. In [`config/feeds.yaml`](config/feeds.yaml), set `site.base_url` to the actual
   Pages URL, with no trailing slash. This repository is already configured for
   `https://dacarlin.github.io/feed-me`. A fork uses
   `https://YOUR-USERNAME.github.io/YOUR-REPOSITORY`.
3. Open the repository on GitHub → **Settings → Pages → Build and deployment →
   Source**, and select **GitHub Actions**. Skip GitHub's suggested starter
   workflow; this project already includes one. This follows GitHub's
   [custom-workflow setup](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site).
4. Under **Settings → Actions → General**, ensure the supplied GitHub-owned
   actions are allowed. The workflow requests `contents: write` only for its
   update job and `pages: write` / `id-token: write` only for deployment. It uses
   the automatic `GITHUB_TOKEN`; no personal access token or custom secret is
   needed. Organization policy or a rule requiring pull requests on the default
   branch can prevent the bot's state commits; this simple workflow assumes
   direct bot pushes are permitted.
5. Optionally add your contact email under **Settings → Secrets and variables →
   Actions → Variables → New repository variable**, named `NCBI_EMAIL`. The code
   sends this and the `pubtracker` tool name to NCBI. Locally, set the same
   environment variable or `update.ncbi_email` in YAML.
6. Open **Actions → Update publication feeds → Run workflow**, select the
   default branch, and click **Run workflow**. Optionally supply `since` to
   backfill an earlier date even when checkpoints already exist. The update job tests the code,
   fetches metadata, commits state/generated changes and uploads `site/`. The
   deploy job publishes the artifact. Inspect both jobs; a reported source
   failure can coexist with a successful deployment of the other sources.
7. Visit `https://dacarlin.github.io/feed-me/` (or your configured URL) and verify
   the XML links load. Code, configuration and update-workflow pushes to `main`
   also trigger an update and deployment; generated state commits do not.
   Subsequent updates are scheduled at minute 17 every three
   hours in UTC. GitHub may delay scheduled jobs. Public repositories can have
   schedules disabled after 60 days of inactivity; re-enable the workflow in
   Actions if necessary. See GitHub's [schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

The workflow serializes updates and never force-pushes. A concurrent human push
can make the bot's push fail; rerun the workflow on the current default branch.
Checkpoint timestamps normally change on every successful run, so periodic
state-only commits are expected even when feed contents remain unchanged.

## Subscribe in NetNewsWire

After the first Pages deployment:

1. On the Mac, choose **File → New Feed** (⌘N; some versions label it **New Web
   Feed**), or click the add-feed button.
2. Paste one of these complete URLs:
   - All configured researchers: `https://dacarlin.github.io/feed-me/all.xml`
   - David Baker: `https://dacarlin.github.io/feed-me/david-baker.xml`
3. Choose your account/folder and click **Add**. Use **On My Mac** for a local-only
   subscription or your existing sync account. Refresh to read new entries.

Replace the host/path for a fork. A `.xml` extension is intentional: these are
Atom feeds, which NetNewsWire accepts. The project index is just a directory of
feed links. NetNewsWire's [add-feed instructions](https://netnewswire.com/help/mac/5.0/en/adding-feeds.html)
describe the same URL subscription flow.

## API behavior and limitations

- Requests are sequential with timeouts and up to three attempts for transient
  HTTP/network failures, exponential backoff, and `Retry-After` handling. A long
  requested retry delay defers the stream to the next scheduled run.
- NCBI requests are spaced by at least 0.4 seconds (2.5/second), below its
  [three-per-second unauthenticated limit](https://www.ncbi.nlm.nih.gov/books/NBK25497/).
  arXiv requests are at least 3.1 seconds apart, following its
  [API rate limits](https://info.arxiv.org/help/api/tou.html). bioRxiv requests
  are at least one second apart. Avoid simultaneous local and Actions updates.
- Discovery is bounded by the configured date windows. Metadata can be missing,
  wrong or arrive late. Weekly refreshes help for already tracked works; papers
  missed entirely may require `--since` or stronger configured evidence.
- There is no Crossref/OpenAlex enrichment, PDF parsing, exhaustive lab-membership
  inference, or reference-list DOI matching. A lab feed is a configured union of
  researchers. Missing explicit publication links and substantial title changes
  can leave separate works. Retractions are not comprehensively tracked.
- bioRxiv requests explicitly select the `/json` format on every page. During
  verification on 2026-09-11 UTC, the implicit-format weekly details URL timed
  out while the same interval with `/json` returned metadata. Genuine outages
  still retain the affected checkpoint and remain visible as workflow failures.
- Feed metadata includes abstracts returned by the APIs, not full articles.
  GitHub Pages output is static and publicly readable. The repository needs no
  application server, database service or authentication system.

Adapters follow the [bioRxiv API](https://api.biorxiv.org/), [NCBI E-utilities](https://www.ncbi.nlm.nih.gov/books/NBK25501/)
and [arXiv API manual](https://info.arxiv.org/help/api/user-manual.html). The
bioRxiv publications parser accepts both the documented `biorxiv_doi` and the
live endpoint's `preprint_doi` field.
