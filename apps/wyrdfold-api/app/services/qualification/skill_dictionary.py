"""Catalog-wide skill extraction by dictionary — no LLM, no per-job cost.

Backs `/search?skill=react`. A skill filter can only find what something wrote
down, and the honest observation is that **the skills people search for are
literal strings already sitting in `jobs.description_html`**. Paying a model to
read text we already store, once per job, forever, is the expensive way to
learn that a posting says "React".

MEASURED against 748 jobs that carry LLM-extracted skills (2026-08-15) — the
LLM's own output as ground truth, scored for free:

                        LLM (paid)      dictionary (free)
    cost                ~$16/mo         $0
    skills / job        6.2             7.7
    jobs with >=1       100%            91.7%
    catalog coverage    forward-only    all 16,625, instantly

The dictionary finds MORE because the LLM's 8-item cap was the binding
constraint, not its ability. Spot-checked the disagreements: on a Full-Stack
posting the model returned 8 terms while the JD literally listed "React,
Angular, Vue", "MySQL, MongoDB", "AWS, GCP, Azure" — every extra the
dictionary found was really there.

WHY THIS BEATS A PROMPT for the facet specifically:
- canonical BY CONSTRUCTION. The LLM's vocabulary fragmented (1,757 distinct
  values, 68% appearing once; `postgres` vs `postgresql`, `ml` vs `machine
  learning`, and a `claud` typo that would be an unclickable dead facet). Here
  the alias map IS the vocabulary, so fragmentation cannot occur.
- RETROACTIVE AND FREE to improve. Adding a term re-enriches every historical
  posting on the next scan at zero cost. Improving a prompt only helps jobs
  processed after the change, unless you re-pay for the whole catalog.
- no classification risk. The earlier attempt to fold skills into the
  qualification tagger's prompt cost ~4 points of `role_family` accuracy
  (95.0% -> 90.7% over 10 golden runs), and role_family gates which jobs reach
  users. This touches no prompt at all.

WHERE THE LLM STILL EARNS ITS KEEP — the Phase-2 harvest, which extracts
skills as a byproduct of grading we already pay for. It covers exactly this
module's two blind spots: inferred skills the text never literally names, and
open-ended domain crafts on non-technical postings (a Product Manager JD that
yields `healthcare, discovery` to a model yields little here). Dictionary =
exhaustive breadth across the catalog; harvest = judgment on graded jobs.

GROWING THE VOCABULARY is a first-class loop, not a someday-chore — see
``skill_growth.py``: harvest terms the dictionary doesn't know, unmatched user
search queries (proven demand), and per-family coverage all generate
candidates for free from data already being collected.
"""

from __future__ import annotations

import re

# canonical name -> surface forms found in real postings.
#
# Seeded from the LLM's own frequency table over 748 jobs, so this is
# discovered vocabulary rather than a guess. Rules for editing:
#   * the KEY is what users search and what lands in the DB — keep it the
#     common spoken form ("postgresql", not "PostgreSQL 15").
#   * forms are matched case-insensitively with word boundaries, so add the
#     ALIASES that fragmented the LLM's output (postgres, k8s, golang).
#   * do NOT add field-wide categories ("ai", "cloud", "engineering",
#     "automation"): they match nearly every posting in a discipline, so they
#     cannot narrow a search. That judgment is made HERE, once, under review —
#     which is the point of a dictionary over a prompt.
#   * avoid bare tokens too ambiguous to disambiguate by word boundary alone
#     ("go", "r", "c" as a language): alias them via unambiguous forms
#     ("golang") or leave them out. A false facet is worse than a missing one.
SKILL_DICTIONARY: dict[str, tuple[str, ...]] = {
    # --- languages -------------------------------------------------------
    "typescript": ("typescript",),
    "javascript": ("javascript", "ecmascript"),
    "python": ("python",),
    "java": ("java",),
    "kotlin": ("kotlin",),
    "swift": ("swift",),
    "go": ("golang", "go lang"),  # bare "go" is unusably ambiguous in prose
    "rust": ("rust",),
    "c++": (r"c\+\+", "cpp"),
    "c#": ("c#", "csharp"),
    ".net": (r"\.net", "dotnet"),
    "php": ("php",),
    "ruby": ("ruby", "ruby on rails", "rails"),
    "scala": ("scala",),
    "perl": ("perl",),
    "r": (r"\br programming\b",),
    "matlab": ("matlab",),
    "verilog": ("verilog",),
    "vhdl": ("vhdl",),
    "solidity": ("solidity",),
    # --- web / frontend --------------------------------------------------
    "react": ("react", "react.js", "reactjs"),
    "next.js": ("next.js", "nextjs"),
    "angular": ("angular", "angularjs"),
    "vue": ("vue", "vue.js", "vuejs"),
    "svelte": ("svelte", "sveltekit"),
    "node.js": ("node.js", "nodejs"),
    "html": ("html", "html5"),
    "css": ("css", "css3"),
    "tailwind css": ("tailwind",),
    "sass": ("sass", "scss"),
    "webpack": ("webpack",),
    "vite": ("vite",),
    "react native": ("react native",),
    "flutter": ("flutter",),
    "ios": ("ios",),
    "android": ("android",),
    # --- data stores -----------------------------------------------------
    "sql": ("sql",),
    "postgresql": ("postgresql", "postgres", "psql"),
    "mysql": ("mysql",),
    "mongodb": ("mongodb", "mongo"),
    "redis": ("redis",),
    "elasticsearch": ("elasticsearch", "elastic search", "opensearch"),
    "kafka": ("kafka",),
    "rabbitmq": ("rabbitmq",),
    "snowflake": ("snowflake",),
    "databricks": ("databricks",),
    "dynamodb": ("dynamodb",),
    "sql server": ("sql server", "mssql"),
    "oracle": ("oracle database", "oracle db"),
    "cassandra": ("cassandra",),
    "neo4j": ("neo4j",),
    # --- cloud / infra ---------------------------------------------------
    "aws": ("aws", "amazon web services"),
    "azure": ("azure",),
    "gcp": ("gcp", "google cloud"),
    "kubernetes": ("kubernetes", "k8s"),
    "docker": ("docker",),
    "terraform": ("terraform",),
    "ansible": ("ansible",),
    "helm": ("helm",),
    "jenkins": ("jenkins",),
    "github actions": ("github actions",),
    "gitlab": ("gitlab",),
    "argocd": ("argocd", "argo cd"),
    "prometheus": ("prometheus",),
    "grafana": ("grafana",),
    "datadog": ("datadog",),
    "splunk": ("splunk",),
    "kibana": ("kibana",),
    "linux": ("linux", "unix"),
    "eks": ("eks",),
    "vmware": ("vmware", "vsphere"),
    "active directory": ("active directory",),
    "powershell": ("powershell",),
    "bash": ("bash", "shell scripting"),
    "git": ("git",),
    "nginx": ("nginx",),
    "ci/cd": ("ci/cd", "cicd", "continuous integration"),
    "infrastructure as code": ("infrastructure as code", "infrastructure-as-code"),
    "microservices": ("microservices", "microservice"),
    "distributed systems": ("distributed systems",),
    "gitops": ("gitops",),
    "devsecops": ("devsecops",),
    "sre": ("site reliability", "sre"),
    "incident response": ("incident response",),
    "observability": ("observability",),
    "graphql": ("graphql",),
    "grpc": ("grpc",),
    "rest": ("rest api", "restful"),
    # --- data / ml -------------------------------------------------------
    "machine learning": ("machine learning",),
    "deep learning": ("deep learning",),
    "natural language processing": ("natural language processing", "nlp"),
    "computer vision": ("computer vision",),
    "llm": ("llm", "llms", "large language model"),
    "pytorch": ("pytorch",),
    "tensorflow": ("tensorflow",),
    "jax": ("jax",),
    "cuda": ("cuda",),
    "pandas": ("pandas",),
    "numpy": ("numpy",),
    "spark": ("apache spark", "pyspark"),
    "airflow": ("airflow",),
    "dbt": ("dbt",),
    "tableau": ("tableau",),
    "power bi": ("power bi", "powerbi"),
    "looker": ("looker",),
    "data pipelines": ("data pipeline", "data pipelines", "etl"),
    # --- testing ---------------------------------------------------------
    "jest": ("jest",),
    "playwright": ("playwright",),
    "cypress": ("cypress",),
    "selenium": ("selenium",),
    "pytest": ("pytest",),
    # --- backend frameworks ----------------------------------------------
    "fastapi": ("fastapi",),
    "django": ("django",),
    "flask": ("flask",),
    "spring": ("spring boot", "spring framework"),
    "express": ("express.js", "expressjs"),
    # --- design ----------------------------------------------------------
    "figma": ("figma",),
    "sketch": ("sketch app",),
    "adobe creative cloud": ("adobe creative cloud", "creative cloud"),
    "photoshop": ("photoshop",),
    "illustrator": ("illustrator",),
    "user research": ("user research",),
    "information architecture": ("information architecture",),
    "wireframing": ("wireframe", "wireframing", "wireframes"),
    "prototyping": ("prototyping", "prototypes"),
    "design systems": ("design system", "design systems"),
    "accessibility": ("accessibility", "wcag", "a11y"),
    "usability testing": ("usability testing",),
    # --- product / process -----------------------------------------------
    "agile": ("agile",),
    "scrum": ("scrum",),
    "jira": ("jira",),
    "confluence": ("confluence",),
    "a/b testing": ("a/b testing", "ab testing", "split testing"),
    "user stories": ("user stories",),
    # --- business tools / domains ----------------------------------------
    "salesforce": ("salesforce",),
    "hubspot": ("hubspot",),
    "servicenow": ("servicenow",),
    "workday": ("workday",),
    "sap": ("sap",),
    "netsuite": ("netsuite",),
    "quickbooks": ("quickbooks",),
    "excel": ("excel",),
    "google analytics": ("google analytics",),
    "seo": ("seo",),
    "paid search": ("paid search", "search engine marketing"),
    "wordpress": ("wordpress",),
    "shopify": ("shopify",),
    "stripe": ("stripe",),
    "twilio": ("twilio",),
    "auth0": ("auth0",),
    "okta": ("okta",),
    "financial modeling": ("financial modeling", "financial modelling"),
    "forecasting": ("forecasting",),
    "gaap": ("gaap",),
    "financial reporting": ("financial reporting",),
    # --- hardware / embedded ---------------------------------------------
    "embedded systems": ("embedded systems", "embedded software"),
    "firmware": ("firmware",),
    "robotics": ("robotics",),
    "rtl design": ("rtl design",),
    "fpga": ("fpga",),
    "semiconductor": ("semiconductor",),
}

# Bound what one job can carry. The LLM's cap of 8 was its binding constraint;
# a facet wants breadth, so this is deliberately looser — but still bounded so
# a stack-listing JD can't write an unbounded blob.
MAX_SKILLS_PER_JOB = 15

# How much text to scan. Free (local regex), so this is the FULL description
# rather than a cost-driven snippet — the reason the LLM path needed a
# 2,000-char window at all was that reading cost money.
_MAX_SCAN_CHARS = 20_000

_TAG_RE = re.compile(r"<[^>]+>")
_ENTITIES = (("&amp;", "&"), ("&nbsp;", " "), ("&#39;", "'"), ("&quot;", '"'), ("&lt;", "<"))


def _compile(forms: tuple[str, ...]) -> re.Pattern[str]:
    """One alternation per canonical skill, bounded so "java" cannot match
    "javascript" and "sap" cannot match "sapphire".

    Custom boundaries (not ``\\b``) because several real skill names end in a
    non-word character — ``c++``, ``c#``, ``ci/cd`` — where ``\\b`` behaves
    backwards. Lookarounds on the alphanumeric class give the same protection
    for those.

    The ``(?:...)`` GROUP is load-bearing, not style. Alternation binds looser
    than concatenation, so an ungrouped body makes the lookarounds apply to
    only the first and last form: ``(?<!x)sem|paid search(?!y)`` guards the
    start of "sem" and the end of "paid search" and nothing else — which let
    ``sem`` match inside "semiconductor" on real postings. Caught by driving
    the live catalog, not by the unit tests, because every boundary case they
    covered happened to be a single-form entry.
    """
    body = "|".join(forms)
    return re.compile(rf"(?<![a-z0-9])(?:{body})(?![a-z0-9+#])", re.IGNORECASE)


_PATTERNS: dict[str, re.Pattern[str]] = {
    canon: _compile(forms) for canon, forms in SKILL_DICTIONARY.items()
}

VOCABULARY: frozenset[str] = frozenset(SKILL_DICTIONARY)


def strip_html(raw: str | None) -> str:
    """Cheap tag/entity strip + whitespace collapse for matching."""
    text = _TAG_RE.sub(" ", raw or "")
    for entity, char in _ENTITIES:
        text = text.replace(entity, char)
    return " ".join(text.split())


def extract_skills(title: str | None, description_html: str | None) -> list[str]:
    """Canonical skills named by this posting. Deterministic, no LLM, no cost.

    Scans title + description. Returns canonical names sorted for a stable
    write (so an unchanged posting produces an unchanged column and the
    content-hash skip stays meaningful).
    """
    haystack = f"{title or ''}\n{strip_html(description_html)[:_MAX_SCAN_CHARS]}"
    found = [canon for canon, pattern in _PATTERNS.items() if pattern.search(haystack)]
    return sorted(found)[:MAX_SKILLS_PER_JOB]


def unknown_terms(candidates: object) -> list[str]:
    """Candidate skill strings that the dictionary does NOT already know.

    The growth loop's primitive: feed it terms discovered elsewhere (Phase-2
    harvest output, user search queries) and it returns the ones worth
    considering for the vocabulary. Tolerant of junk input — anything that is
    not a usable string is skipped rather than raising.
    """
    if not isinstance(candidates, (list, tuple, set, frozenset)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        if not isinstance(raw, str):
            continue
        term = " ".join(raw.lower().split())
        if not term or term in VOCABULARY or term in seen:
            continue
        seen.add(term)
        out.append(term)
    return out
