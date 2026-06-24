"""Job qualification firewall (#60).

A cheap, cached, target-INDEPENDENT tagger that classifies each job ONCE and
stores the intrinsic facts on the ``jobs`` row, so per-target grading
(Phase 1 title triage + Phase 2 fit) can pre-filter cheaply instead of paying
N targets to re-learn the same facts about one posting.

Two layers, mirroring ``relevance`` / ``fit``:

- ``heuristics`` (L1): pure Python, no LLM — HTML/entity stripping, the
  permissive US-location guess (the canonical home for the poller's old
  ingestion gate), and the content hash that lets the tagger skip unchanged
  rows.
- ``tagger`` (L2): ONE structured Haiku call per job returning a
  ``QualificationTags`` object that maps 1:1 onto the qualification columns.

Gated behind ``settings.qualification_enabled`` (default off), so merging the
package and migration triggers no LLM spend.
"""

from app.services.qualification.heuristics import (
    clean_description,
    is_us_location,
    qualification_hash,
)
from app.services.qualification.tagger import (
    QUALIFICATION_MODEL,
    QUALIFICATION_PURPOSE,
    EmploymentType,
    QualificationTags,
    RoleFamily,
    Seniority,
    tag_job,
)

__all__ = [
    "QUALIFICATION_MODEL",
    "QUALIFICATION_PURPOSE",
    "EmploymentType",
    "QualificationTags",
    "RoleFamily",
    "Seniority",
    "clean_description",
    "is_us_location",
    "qualification_hash",
    "tag_job",
]
