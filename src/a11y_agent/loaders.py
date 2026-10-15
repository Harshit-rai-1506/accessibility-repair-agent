"""Read the downloaded resources as LangChain Documents.

A LangChain ``Document`` is a piece of text (``page_content``) plus a dict of
facts about it (``metadata``). Everything later in the pipeline speaks this
format: text splitters cut Documents into chunks, vector stores index them,
retrievers return them, and evaluation runs iterate over them. So once
``scripts/fetch_resources.py`` has downloaded the files, the next step is to
turn each resource into Documents. That is what this module does.

Three resources, three loaders:

``BenchmarkLoader``
    One Document per benchmark row. ``page_content`` is the broken HTML snippet;
    metadata holds the rule it violates, its impact and the page it came from.

``WCAGLoader``
    One Document per W3C page (Understanding documents, success criteria and
    Techniques), converted to Markdown, with metadata saying which WCAG success
    criteria the page is about. Uses a per-page loader (``WCAGPageLoader``)
    and a document transformer (``MarkdownTransformer``).

``AxeRuleLoader``
    One Document per axe-core rule: what it checks, what makes an element pass,
    and which WCAG criteria it maps to.

All of them implement LangChain's standard interfaces from ``langchain-core``:
loaders subclass ``BaseLoader`` and yield Documents from ``lazy_load()``;
the Markdown step subclasses ``BaseDocumentTransformer``. We do not use the
ready-made ``CSVLoader`` / ``DirectoryLoader`` / ``MarkdownifyTransformer``,
because they live in ``langchain-community``, which LangChain sunset in 2026
and no longer maintains.

All metadata values are plain strings, numbers or booleans, because vector
stores such as Chroma reject lists and nested dicts. Lists of WCAG numbers are
stored as comma-separated strings, e.g. ``"2.4.4,4.1.2"``.

Try it from the repository root:

    python -m a11y_agent.loaders                      # counts for every loader
    python -m a11y_agent.loaders --show wcag --limit 1
    python -m a11y_agent.loaders --show benchmark --limit 3
    python -m a11y_agent.loaders --show axe --limit 2
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from bs4 import BeautifulSoup, Comment
from langchain_core.document_loaders import BaseLoader
from langchain_core.documents import BaseDocumentTransformer, Document
from markdownify import markdownify

from .config import settings
from .scoring import IMPACT_SCORE


def _relative_to_data(path: Path) -> str:
    """Paths in metadata are relative to data/, so they are the same on every machine."""
    try:
        return path.resolve().relative_to(settings.data_dir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------------------
# 1. Benchmark rows
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BenchmarkSpec:
    """Where one benchmark CSV lives and which of its columns mean what.

    The three CSVs come from different papers and name their columns
    differently; this table maps them onto the same metadata keys.
    """
    path: str                         # relative to data/benchmark/accessguru/data
    html_column: str                  # the failing HTML snippet
    rule_column: str                  # axe rule id, e.g. "image-alt"
    impact_column: str                # "critical" | "serious" | "moderate" | "minor"
    url_column: str                   # page the snippet came from
    category_column: str | None = None
    score_column: str | None = None   # precomputed impact score, if the CSV has one
    html_is_python_list: bool = False  # the ACCESS CSV stores "[['<a ...>']]"


BENCHMARKS: dict[str, BenchmarkSpec] = {
    # AccessGuru (ASSETS 2025): 305 rows, 195 syntax + 55 layout + 55 semantic
    "accessguru": BenchmarkSpec(
        path="accessguru_dataset/accessguru_sampled_syntax_layout_dataset.csv",
        html_column="affectedHTMLElement(s)",
        rule_column="violationnumberID",
        impact_column="impact",
        url_column="webURL",
        category_column="category",
        score_column="initialImpactScore",
    ),
    # The same 55 semantic violations, with AccessGuru's own violation names
    "accessguru-semantic": BenchmarkSpec(
        path="accessguru_dataset/accessguru_sampled_semantic_violations.csv",
        html_column="Affected HTML",
        rule_column="Violation Type",
        impact_column="Impact",
        url_column="webURL",
    ),
    # ACCESS (Huang et al. 2024): 171 rows from about 25 popular sites
    "access": BenchmarkSpec(
        path="baseline_two_dataset.csv",
        html_column="html",
        rule_column="id",
        impact_column="impact",
        url_column="webURL",
        html_is_python_list=True,
    ),
}


def _join_python_list(text: str) -> str:
    """ACCESS stores snippets as a printed Python list, e.g. "[['<img ...>']]".
    Turn that back into plain HTML, one element per line."""
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text

    def flatten(v) -> Iterable[str]:
        if isinstance(v, (list, tuple)):
            for item in v:
                yield from flatten(item)
        else:
            yield str(v)

    return "\n".join(flatten(value))


class BenchmarkLoader(BaseLoader):
    """Load one benchmark CSV as Documents, one per violation.

    >>> docs = BenchmarkLoader("accessguru", categories=["Syntax", "Layout"]).load()
    >>> docs[0].page_content          # the broken HTML snippet
    >>> docs[0].metadata["rule_id"]   # e.g. "color-contrast-enhanced"

    Each CSV row becomes one Document. The three datasets name their columns
    differently, so ``BENCHMARKS`` maps them onto the same metadata keys, and
    ``page_content`` is exactly the HTML the scanner needs, nothing added.
    """

    def __init__(self, dataset: str = "accessguru", *, categories: Iterable[str] | None = None,
                 benchmark_dir: Path | None = None):
        if dataset not in BENCHMARKS:
            raise ValueError(f"unknown dataset {dataset!r}; choose from {sorted(BENCHMARKS)}")
        self.dataset = dataset
        self.spec = BENCHMARKS[dataset]
        self.categories = {c.lower() for c in categories} if categories else None
        base = benchmark_dir or settings.benchmark_dir / "accessguru" / "data"
        self.path = Path(base) / self.spec.path

    def lazy_load(self) -> Iterator[Document]:
        spec = self.spec
        if not self.path.exists():
            raise FileNotFoundError(f"{self.path} not found. Run: python scripts/fetch_resources.py --only benchmark")

        # A few rows hold large HTML; lift Python's CSV field size limit
        # (2**31 - 1 is the largest value Windows accepts).
        csv.field_size_limit(2**31 - 1)

        source = _relative_to_data(self.path)
        # encoding="utf-8": without it, Windows reads the file as cp1252 and fails.
        # newline="": lets the csv module handle line breaks inside quoted HTML.
        with open(self.path, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))

        for row, raw in enumerate(rows):
            category = (raw.get(spec.category_column) or "").strip() if spec.category_column else ""
            if self.categories is not None and category.lower() not in self.categories:
                continue

            html = (raw.get(spec.html_column) or "").strip()
            if spec.html_is_python_list:
                html = _join_python_list(html)

            impact = (raw.get(spec.impact_column) or "").strip().lower()
            score_text = (raw.get(spec.score_column) or "").strip() if spec.score_column else ""
            impact_score = int(float(score_text)) if score_text else IMPACT_SCORE.get(impact, 0)

            yield Document(
                id=f"{self.dataset}-{row:03d}",
                page_content=html,
                metadata={
                    "source": source,
                    "dataset": self.dataset,
                    "row": row,
                    "rule_id": (raw.get(spec.rule_column) or "").strip(),
                    "category": category,
                    "impact": impact,
                    "impact_score": impact_score,
                    "url": (raw.get(spec.url_column) or "").strip(),
                },
            )


# ---------------------------------------------------------------------------
# 2. WCAG documents
# ---------------------------------------------------------------------------

WCAG_TR_URL = "https://www.w3.org/TR/WCAG22/"
UNDERSTANDING_URL = "https://www.w3.org/WAI/WCAG22/Understanding/"
TECHNIQUES_URL = "https://www.w3.org/WAI/WCAG22/Techniques/"
VERSION_DIRS = {"20": "2.0", "21": "2.1", "22": "2.2"}

# Values the W3C site fills in when it builds the pages
TEMPLATE_VALUES = {"versionDecimal": "2.2", "version": "22", "trUrl": WCAG_TR_URL}
TEMPLATE_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")
TEMPLATE_TAG = re.compile(r"\{%.*?%\}", re.S)
TECHNIQUES_INCLUDE = '{% include "understanding/techniques.html" %}'

TECHNIQUE_ID = re.compile(r'"((?:ARIA|C|F|FLASH|G|H|PDF|SCR|SL|SM|SVR|T)\d+)"')
TECHNIQUE_CATEGORIES = ("sufficient", "advisory", "failure")
CATEGORY_HEADINGS = {"sufficient": "Sufficient techniques", "advisory": "Advisory techniques",
                     "failure": "Failures"}
DOC_TYPES = ("understanding", "success_criterion", "technique", "failure")

# Every metadata key a WCAG Document can have. All are always present (""
# when not applicable), so every Document has the same shape.
WCAG_METADATA_KEYS = ("source", "url", "title", "doc_type", "sc", "sc_title", "guideline",
                      "level", "wcag_version", "technique_id", "technology")


@dataclass
class Criterion:
    """One WCAG success criterion, e.g. 2.5.8 Target Size (Minimum), Level AA."""
    number: str        # "2.5.8"
    slug: str          # "target-size-minimum" (file name and URL fragment)
    title: str         # "Target Size (Minimum)"
    level: str         # "A", "AA", "AAA" ("" if removed)
    version: str       # WCAG version that introduced it: "2.0", "2.1" or "2.2"
    obsolete: bool = False

    @property
    def guideline(self) -> str:
        return self.number.rsplit(".", 1)[0]

    def label(self) -> str:
        level = f" (Level {self.level})" if self.level else ""
        return f"{self.number} {self.title}{level}"


@dataclass
class WCAGIndex:
    """Facts about WCAG that individual pages do not state themselves.

    The source files never say "this is success criterion 2.5.8": the number
    comes from the page's position in guidelines/index.html, and the list of
    techniques for each criterion lives in a separate data file. This index
    reads both once so each page can be labelled correctly.
    """
    criteria: dict[str, Criterion] = field(default_factory=dict)          # by slug
    guidelines: dict[str, tuple[str, str]] = field(default_factory=dict)  # slug -> (number, title)
    techniques: dict[str, dict[str, list[str]]] = field(default_factory=dict)  # sc slug -> category -> ids
    technique_titles: dict[str, str] = field(default_factory=dict)  # downloaded techniques only
    obsolete_techniques: set[str] = field(default_factory=set)

    @classmethod
    def build(cls, corpus_dir: Path) -> "WCAGIndex":
        corpus_dir = Path(corpus_dir)
        index_file = corpus_dir / "guidelines" / "index.html"
        if not index_file.exists():
            raise FileNotFoundError(f"{index_file} not found. Run: python scripts/fetch_resources.py --only wcag")
        idx = cls()

        # Numbering: the n-th principle, n-th guideline in it, n-th criterion in that.
        soup = BeautifulSoup(index_file.read_text(encoding="utf-8"), "html.parser")
        for p_num, principle in enumerate(soup.select("section.principle"), 1):
            for g_num, guideline in enumerate(principle.select("section.guideline"), 1):
                heading = guideline.find(["h3", "h2", "h4"])
                idx.guidelines[guideline["id"]] = (f"{p_num}.{g_num}", _text(heading) if heading else "")
                for s_num, include in enumerate(guideline.select("section[data-include]"), 1):
                    rel = include["data-include"]                     # "sc/22/target-size-minimum.html"
                    version_dir, file_name = rel.split("/")[-2:]
                    slug = file_name.removesuffix(".html")
                    sc_html = BeautifulSoup((corpus_dir / "guidelines" / rel).read_text(encoding="utf-8"), "html.parser")
                    title_el = sc_html.find(["h4", "h3", "h2"])
                    level_el = sc_html.select_one("p.conformance-level")
                    title = _text(title_el) if title_el else slug
                    idx.criteria[slug] = Criterion(
                        number=f"{p_num}.{g_num}.{s_num}",
                        slug=slug,
                        title=re.sub(r"\s*\(Obsolete and removed\)", "", title),
                        level=level_el.get_text(strip=True) if level_el else "",
                        version=VERSION_DIRS.get(version_dir, ""),
                        obsolete="obsolete" in title.lower(),
                    )

        idx.techniques = _parse_associated_techniques(corpus_dir / "understanding" / "understanding.11tydata.js")
        for path in (corpus_dir / "techniques").glob("*/*.html"):
            front_matter, text = _split_front_matter(path.read_text(encoding="utf-8"))
            if "obsoleteSince" in front_matter:
                idx.obsolete_techniques.add(path.stem)
            match = re.search(r"<h1[^>]*>(.*?)</h1>", text, re.S)
            if match:
                idx.technique_titles[path.stem] = _text(BeautifulSoup(match.group(1), "html.parser"))
        return idx

    def criteria_for_technique(self, technique_id: str) -> list[tuple[Criterion, str]]:
        """Which criteria list this technique, and how (sufficient, advisory or failure)."""
        found = []
        for slug, by_category in self.techniques.items():
            criterion = self.criteria.get(slug)
            if criterion is None:
                continue
            for category in TECHNIQUE_CATEGORIES:
                if technique_id in by_category.get(category, []):
                    found.append((criterion, category))
                    break
        return sorted(found, key=lambda item: [int(n) for n in item[0].number.split(".")])

    def by_number(self, number: str) -> Criterion | None:
        return next((c for c in self.criteria.values() if c.number == number), None)


def _parse_associated_techniques(path: Path) -> dict[str, dict[str, list[str]]]:
    """Read which techniques belong to which criterion.

    The W3C keeps this in a JavaScript data file (understanding.11tydata.js),
    laid out like:

        associatedTechniques: {
          "non-text-content": {
            sufficient: [ { techniques: ["G94"], groups: [ { techniques: ["H37", ...] } ] } ],
            failure: ["F3", "F13", ...],
          },

    It is not JSON, so instead of evaluating JavaScript we read it line by
    line: a 6-space-indented key starts a criterion, a "sufficient:" /
    "advisory:" / "failure:" key switches category, and every quoted
    technique id after that belongs to it.
    """
    text = path.read_text(encoding="utf-8")
    start = text.find("associatedTechniques:")
    if start < 0:
        return {}
    body = text[start:]
    criterion_start = re.compile(r'^ {6}"?([a-z0-9-]+)"?: \{', re.M)
    category_start = re.compile(r"^\s+(sufficient|advisory|failure):", re.M)

    result: dict[str, dict[str, list[str]]] = {}
    heads = list(criterion_start.finditer(body))
    for i, head in enumerate(heads):
        block = body[head.end(): heads[i + 1].start() if i + 1 < len(heads) else len(body)]
        events = sorted(
            [(m.start(), "category", m.group(1)) for m in category_start.finditer(block)]
            + [(m.start(), "technique", m.group(1)) for m in TECHNIQUE_ID.finditer(block)]
        )
        by_category: dict[str, list[str]] = defaultdict(list)
        category = None
        for _, kind, value in events:
            if kind == "category":
                category = value
            elif category and value not in by_category[category]:
                by_category[category].append(value)
        result[head.group(1)] = dict(by_category)
    return result


def _split_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Some technique files start with a YAML block between '---' lines
    (e.g. 'obsoleteSince: 20'). Return its top-level keys and the rest."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    keys = {}
    for line in text[3:end].splitlines():
        if re.match(r"^[A-Za-z]\w*:", line):
            key, _, value = line.partition(":")
            keys[key] = value.strip()
    return keys, text[end + 4:]


def _text(el) -> str:
    """Visible text of an element on one line."""
    return re.sub(r"\s+", " ", el.get_text(" ")).strip()


def _clean_html(text: str, techniques_html: str = "") -> BeautifulSoup:
    """Fill template values and drop build-only markup (head, comments, page metadata)."""
    text = text.replace(TECHNIQUES_INCLUDE, techniques_html)
    text = TEMPLATE_VAR.sub(lambda m: TEMPLATE_VALUES.get(m.group(1), ""), text)
    text = TEMPLATE_TAG.sub("", text)
    soup = BeautifulSoup(text, "html.parser")
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for el in soup.select("head, link, script, style, section.meta, p.conformance-level, p.change"):
        el.decompose()
    # The source files wrap long lines; join them (except inside code examples).
    for node in soup.find_all(string=True):
        if node.find_parent("pre") is None and re.search(r"\s\s|\n", node):
            node.replace_with(re.sub(r"\s+", " ", str(node)))
    return soup


def _body_html(soup: BeautifulSoup) -> str:
    return str(soup.body or soup)


def _new_tag(soup: BeautifulSoup, name: str, text: str):
    tag = soup.new_tag(name)
    tag.string = text
    return tag


class WCAGPageLoader(BaseLoader):
    """Load one W3C source page as one Document of cleaned HTML plus metadata.

    ``WCAGLoader`` creates one of these per file. Pages that are not about a
    specific criterion or technique (introductions, templates) and obsolete
    material yield nothing.
    """

    def __init__(self, file_path: str, *, index: WCAGIndex, corpus_dir: Path,
                 include_obsolete: bool = False):
        self.path = Path(file_path)
        self.index = index
        self.corpus_dir = Path(corpus_dir)
        self.include_obsolete = include_obsolete

    def lazy_load(self) -> Iterator[Document]:
        rel = self.path.relative_to(self.corpus_dir).as_posix()
        front_matter, text = _split_front_matter(self.path.read_text(encoding="utf-8"))
        section = rel.split("/")[0]
        if section == "understanding":
            doc = self._understanding(rel, text)
        elif section == "guidelines":
            doc = self._success_criterion(rel, text)
        elif section == "techniques":
            if "obsoleteSince" in front_matter and not self.include_obsolete:
                return
            doc = self._technique(rel, text)
        else:
            doc = None
        if doc is not None:
            yield doc

    # -- helpers ------------------------------------------------------------

    def _metadata(self, rel: str, **values) -> dict:
        metadata = {key: "" for key in WCAG_METADATA_KEYS}
        metadata["source"] = _relative_to_data(self.corpus_dir / rel)
        metadata.update(values)
        return metadata

    def _techniques_section(self, slug: str) -> str:
        """HTML list of the techniques for one criterion. On the W3C website this
        list is inserted when the site is built; the source files only have a
        placeholder, so we rebuild it from the index."""
        by_category = self.index.techniques.get(slug, {})
        parts = ["<section><h2>Techniques</h2>"]
        for category in TECHNIQUE_CATEGORIES:
            # Only list techniques we have pages for (HTML, CSS, ARIA, script,
            # general, text, failures; not PDF or Flash), minus obsolete ones.
            ids = [t for t in by_category.get(category, []) if t in self.index.technique_titles
                   and (self.include_obsolete or t not in self.index.obsolete_techniques)]
            if not ids:
                continue
            items = "".join(
                f"<li>{tid}: {self.index.technique_titles.get(tid, '')}</li>" for tid in ids
            )
            parts.append(f"<h3>{CATEGORY_HEADINGS[category]}</h3><ul>{items}</ul>")
        if len(parts) == 1:
            return ""
        parts.append("</section>")
        return "".join(parts)

    def _understanding(self, rel: str, text: str) -> Document | None:
        parts = rel.split("/")
        if len(parts) != 3 or parts[1] not in VERSION_DIRS:
            return None                        # general pages such as intro.html
        slug = parts[2].removesuffix(".html")
        criterion = self.index.criteria.get(slug)
        guideline = self.index.guidelines.get(slug)
        if criterion is None and guideline is None:
            return None
        if criterion is not None and criterion.obsolete and not self.include_obsolete:
            return None

        soup = _clean_html(text, self._techniques_section(slug) if criterion else "")
        h1 = soup.find("h1")
        title = _text(h1) if h1 else slug
        if criterion is not None:
            intro = (f"Success criterion {criterion.number} {criterion.title}, "
                     f"Level {criterion.level or 'none'}, added in WCAG {criterion.version}.")
            meta = dict(sc=criterion.number, sc_title=criterion.title, guideline=criterion.guideline,
                        level=criterion.level, wcag_version=criterion.version)
        else:
            number, g_title = guideline
            intro = f"Guideline {number} {g_title}."
            meta = dict(guideline=number)
        if h1 is not None:
            h1.insert_after(_new_tag(soup, "p", intro))

        return Document(
            id=rel,
            page_content=_body_html(soup),
            metadata=self._metadata(rel, url=UNDERSTANDING_URL + slug, title=title,
                                    doc_type="understanding", **meta),
        )

    def _success_criterion(self, rel: str, text: str) -> Document | None:
        if not rel.startswith("guidelines/sc/"):
            return None                        # guidelines/index.html itself
        slug = rel.rsplit("/", 1)[1].removesuffix(".html")
        criterion = self.index.criteria.get(slug)
        if criterion is None or (criterion.obsolete and not self.include_obsolete):
            return None

        soup = _clean_html(text)
        old_heading = soup.find(["h4", "h3", "h2"])
        heading = _new_tag(soup, "h1", f"Success Criterion {criterion.number} {criterion.title}")
        if old_heading is not None:
            old_heading.replace_with(heading)
        else:
            soup.insert(0, heading)
        heading.insert_after(_new_tag(soup, "p", f"Level {criterion.level}. Added in WCAG {criterion.version}."))

        return Document(
            id=rel,
            page_content=_body_html(soup),
            metadata=self._metadata(
                rel, url=WCAG_TR_URL + "#" + slug, title=f"{criterion.number} {criterion.title}",
                doc_type="success_criterion", sc=criterion.number, sc_title=criterion.title,
                guideline=criterion.guideline, level=criterion.level, wcag_version=criterion.version,
            ),
        )

    def _technique(self, rel: str, text: str) -> Document | None:
        _, technology, file_name = rel.split("/")
        technique_id = file_name.removesuffix(".html")
        soup = _clean_html(text)
        h1 = soup.find("h1")
        title = _text(h1) if h1 else technique_id
        related = self.index.criteria_for_technique(technique_id)
        doc_type = "failure" if technology == "failures" else "technique"

        if related:
            if doc_type == "failure":
                summary = "; ".join(c.label() for c, _ in related)
                intro = f"Failure {technique_id}. Fails success criterion {summary}."
            else:
                summary = "; ".join(f"{c.label()} [{category}]" for c, category in related)
                intro = f"Technique {technique_id} ({technology}). Used for success criterion {summary}."
        else:
            intro = f"{'Failure' if doc_type == 'failure' else 'Technique'} {technique_id} ({technology})."
        if h1 is not None:
            h1.insert_after(_new_tag(soup, "p", intro))

        criteria = [c for c, _ in related]
        return Document(
            id=rel,
            page_content=_body_html(soup),
            metadata=self._metadata(
                rel, url=f"{TECHNIQUES_URL}{technology}/{technique_id}", title=title, doc_type=doc_type,
                sc=",".join(c.number for c in criteria),
                guideline=",".join(dict.fromkeys(c.guideline for c in criteria)),
                technique_id=technique_id, technology=technology,
            ),
        )


class MarkdownTransformer(BaseDocumentTransformer):
    """Convert HTML Documents to Markdown (a LangChain document transformer).

    Headings become ``#`` / ``##`` lines and ``<pre>`` code becomes fenced
    blocks. Links and images are reduced to their text: their URLs are noise
    for retrieval and for the model.
    """

    def transform_documents(self, documents, **kwargs) -> list[Document]:
        out = []
        for doc in documents:
            md = markdownify(doc.page_content, heading_style="ATX", strip=["a", "img", "svg"],
                             code_language_callback=_code_language)
            md = md.replace("\xa0", " ")
            md = re.sub(r"[ \t]+\n", "\n", md)        # trailing spaces
            md = re.sub(r"\n{3,}", "\n\n", md).strip()  # at most one blank line
            out.append(Document(id=doc.id, page_content=md, metadata=dict(doc.metadata)))
        return out


def _code_language(pre) -> str:
    """Pick the language for a fenced code block from class="language-html"."""
    for el in [pre, *pre.find_all("code")]:
        for cls in el.get("class") or []:
            if cls.startswith("language-"):
                return cls.removeprefix("language-")
    return ""


class WCAGLoader(BaseLoader):
    """Load the W3C WCAG pages as Markdown Documents with WCAG metadata.

    >>> docs = WCAGLoader().load()
    >>> [d for d in docs if d.metadata["sc"] == "2.5.8"]   # everything about target size

    A three-step pipeline:

    1. Find the files (sorted, so the order is the same on every operating
       system) and hand each one to ``WCAGPageLoader``, which cleans the HTML
       and attaches metadata.
    2. Keep only the requested document types.
    3. ``MarkdownTransformer`` converts the HTML to Markdown, keeping headings
       (``#``, ``##``) and code examples (fenced blocks). Markdown headings are
       what Phase 4 will split on, so each chunk stays within one section.
    """

    GLOBS = ["understanding/20/*.html", "understanding/21/*.html", "understanding/22/*.html",
             "guidelines/sc/*/*.html", "techniques/*/*.html"]

    def __init__(self, corpus_dir: Path | None = None, *, doc_types: Iterable[str] = DOC_TYPES,
                 markdown: bool = True, include_obsolete: bool = False, index: WCAGIndex | None = None):
        self.corpus_dir = Path(corpus_dir or settings.corpus_dir / "wcag")
        unknown = set(doc_types) - set(DOC_TYPES)
        if unknown:
            raise ValueError(f"unknown doc_types {sorted(unknown)}; choose from {DOC_TYPES}")
        self.doc_types = tuple(doc_types)
        self.markdown = markdown
        self.include_obsolete = include_obsolete
        self._index = index

    @property
    def index(self) -> WCAGIndex:
        if self._index is None:
            self._index = WCAGIndex.build(self.corpus_dir)
        return self._index

    def files(self) -> list[Path]:
        if not self.corpus_dir.exists():
            raise FileNotFoundError(f"{self.corpus_dir} not found. Run: python scripts/fetch_resources.py --only wcag")
        return sorted({p for pattern in self.GLOBS for p in self.corpus_dir.glob(pattern) if p.is_file()})

    def lazy_load(self) -> Iterator[Document]:
        to_markdown = MarkdownTransformer() if self.markdown else None
        for path in self.files():
            page = WCAGPageLoader(str(path), index=self.index, corpus_dir=self.corpus_dir,
                                  include_obsolete=self.include_obsolete)
            for doc in page.lazy_load():
                if doc.metadata["doc_type"] not in self.doc_types:
                    continue
                yield to_markdown.transform_documents([doc])[0] if to_markdown else doc


# ---------------------------------------------------------------------------
# 3. axe-core rules
# ---------------------------------------------------------------------------

_WCAG_TAG = re.compile(r"^wcag(\d)(\d)(\d+)$")
_PLACEHOLDER = re.compile(r"\$\{[^}]*\}|\{\{[^}]*\}\}")


def _message_text(message) -> list[str]:
    """axe messages are strings or {messageKey: string}; placeholders such as
    ${data.contrastRatio} are filled in at scan time, so we blank them."""
    values = message.values() if isinstance(message, dict) else [message]
    out = []
    for value in values:
        if isinstance(value, str):
            text = re.sub(r"\s+", " ", _PLACEHOLDER.sub("…", value)).strip()
            if text and text not in out:
                out.append(text)
    return out


class AxeRuleLoader(BaseLoader):
    """Load each axe-core rule as a Document explaining how to pass it.

    The agent sees the rule id in a scan (e.g. "image-alt"); this Document
    tells it, in plain words, what the rule checks and which conditions make
    an element pass, straight from axe-core's own messages. It also maps the
    rule to WCAG success criteria, which is how retrieval finds the matching
    W3C pages.
    """

    def __init__(self, rules_dir: Path | None = None, *, index: WCAGIndex | None = None):
        self.rules_dir = Path(rules_dir or settings.corpus_dir / "axe-rules")
        self.index = index

    def lazy_load(self) -> Iterator[Document]:
        template = self.rules_dir / "locales" / "_template.json"
        if not template.exists():
            raise FileNotFoundError(f"{template} not found. Run: python scripts/fetch_resources.py --only axe-rules")
        messages = json.loads(template.read_text(encoding="utf-8"))
        rule_text, check_text = messages.get("rules", {}), messages.get("checks", {})
        major_minor = ".".join(settings.axe_version.split(".")[:2])

        for path in sorted((self.rules_dir / "lib" / "rules").glob("*.json")):
            rule = json.loads(path.read_text(encoding="utf-8"))
            rule_id = rule["id"]
            text = rule_text.get(rule_id) or rule.get("metadata", {})
            criteria = [".".join(m.groups()) for t in rule.get("tags", []) if (m := _WCAG_TAG.match(t))]
            best_practice = "best-practice" in rule.get("tags", [])

            sentences = [t.strip() for t in (text.get("help", ""), text.get("description", "")) if t]
            lines = [f"axe-core rule: {rule_id}", *(t if t.endswith(".") else t + "." for t in sentences)]
            lines.append(f"Impact: {rule.get('impact') or 'varies'}.")
            if criteria:
                labels = []
                for number in criteria:
                    criterion = self.index.by_number(number) if self.index else None
                    labels.append(criterion.label() if criterion else number)
                lines.append("WCAG success criteria: " + "; ".join(labels) + ".")
            if best_practice:
                lines.append("This is a best-practice rule, not a WCAG requirement.")
            if rule.get("selector"):
                lines.append(f"Applies to elements matching: {rule['selector']}")

            def conditions(check_ids, key):
                found = []
                for cid in check_ids:
                    found += [m for m in _message_text(check_text.get(cid, {}).get(key, "")) if m not in found]
                return found

            for heading, ids, key in [
                ("To pass, at least one of these must be true:", rule.get("any", []), "pass"),
                ("To pass, all of these must be true:", rule.get("all", []), "pass"),
                ("It fails if any of these is found:", rule.get("none", []), "fail"),
            ]:
                items = conditions(ids, key)
                if items:
                    lines.append(heading)
                    lines += [f"- {item}" for item in items]

            yield Document(
                id=f"axe:{rule_id}",
                page_content="\n".join(line for line in lines if line),
                metadata={
                    "source": _relative_to_data(path),
                    "url": f"https://dequeuniversity.com/rules/axe/{major_minor}/{rule_id}",
                    "title": text.get("help", rule_id),
                    "doc_type": "axe_rule",
                    "rule_id": rule_id,
                    "impact": rule.get("impact") or "",
                    "sc": ",".join(criteria),
                    "best_practice": best_practice,
                    "enabled": rule.get("enabled", True),
                },
            )


# ---------------------------------------------------------------------------
# Command line: see what the loaders produce
# ---------------------------------------------------------------------------

def _print_docs(docs: list[Document], limit: int, chars: int) -> None:
    for doc in docs[:limit]:
        print("-" * 78)
        print(f"id: {doc.id}")
        print("metadata:", json.dumps(doc.metadata, ensure_ascii=False))
        content = doc.page_content
        print(content[:chars] + (f"\n… ({len(content) - chars} more characters)" if len(content) > chars else ""))
    print("-" * 78)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load the downloaded resources as LangChain Documents.")
    parser.add_argument("--show", choices=["benchmark", "wcag", "axe"], help="print example documents")
    parser.add_argument("--limit", type=int, default=2, help="how many examples to print")
    parser.add_argument("--chars", type=int, default=1200, help="characters of content to print per example")
    args = parser.parse_args(argv)

    # Windows terminals default to a code page that cannot print every character.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    if args.show == "benchmark":
        _print_docs(BenchmarkLoader("accessguru").load(), args.limit, args.chars)
        return 0
    if args.show == "wcag":
        docs = WCAGLoader().load()
        example = [d for d in docs if d.metadata["sc"] == "2.5.8"] or docs
        _print_docs(example, args.limit, args.chars)
        return 0
    if args.show == "axe":
        index = WCAGIndex.build(settings.corpus_dir / "wcag")
        docs = AxeRuleLoader(index=index).load()
        _print_docs([d for d in docs if d.metadata["rule_id"] in ("image-alt", "target-size")], args.limit, args.chars)
        return 0

    for name in BENCHMARKS:
        docs = BenchmarkLoader(name).load()
        cats = Counter(d.metadata["category"] or "uncategorised" for d in docs)
        print(f"benchmark {name:20} {len(docs):4} documents  " + ", ".join(f"{k}: {v}" for k, v in sorted(cats.items())))
    wcag = WCAGLoader()
    docs = wcag.load()
    types = Counter(d.metadata["doc_type"] for d in docs)
    print(f"wcag      {'pages':20} {len(docs):4} documents  " + ", ".join(f"{k}: {v}" for k, v in sorted(types.items())))
    rules = AxeRuleLoader(index=wcag.index).load()
    print(f"axe       {'rules':20} {len(rules):4} documents")
    return 0


if __name__ == "__main__":
    sys.exit(main())
