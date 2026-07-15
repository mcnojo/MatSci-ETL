"""Pydantic schemas for tree-building LLM structured outputs.

RESPONSE_SCHEMAS keys cross the Temporal boundary as strings (Pydantic classes
can't serialize as workflow input). Downstream annotations (appear_start,
list_index) live on the model_dump dict, not the instance — schemas stay strict.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


def _yn(v) -> str:
    # Collapse casing / "true"/"y"/"1" — the LLM's exact spelling drifts across
    # prompt styles and providers; downstream checks compare against "yes" / "no".
    if isinstance(v, bool):
        return "yes" if v else "no"
    if not isinstance(v, str):
        return "no"
    s = v.strip().lower()
    return "yes" if s in ("yes", "y", "true", "1") else "no"


class TocDetection(BaseModel):
    model_config = ConfigDict(extra="ignore")
    toc_detected: str = "no"

    @field_validator("toc_detected", mode="before")
    @classmethod
    def _norm(cls, v):
        return _yn(v)


class PageIndexPresent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    page_index_given_in_toc: str = "no"

    @field_validator("page_index_given_in_toc", mode="before")
    @classmethod
    def _norm(cls, v):
        return _yn(v)


class TocCompletion(BaseModel):
    model_config = ConfigDict(extra="ignore")
    completed: str = "no"

    @field_validator("completed", mode="before")
    @classmethod
    def _norm(cls, v):
        return _yn(v)


# min/max propagate to JSON schema → vLLM guided decoding enforces per-token.
# title min_length=5 admits "Intro"-length labels while rejecting empty and
# single-token cheats. Length bounds sized for long journal titles, depth-2
# structure indices, and <physical_index_XXXXXXX> tags.
_BoundedTitle = Annotated[str, StringConstraints(min_length=5, max_length=300)]
_BoundedStructure = Annotated[str, StringConstraints(max_length=16)]
_BoundedIndex = Annotated[str, StringConstraints(max_length=32)]


class TocItem(BaseModel):
    # extra="forbid" → additionalProperties: false; blocks guided decoding from
    # inventing per-entry keys (primary driver of the 16K runaway).
    # title is required — no default; forces instructor-repair on omission.
    model_config = ConfigDict(extra="forbid")
    title: _BoundedTitle
    structure: _BoundedStructure | None = None
    physical_index: int | _BoundedIndex | None = None
    page: int | _BoundedIndex | None = None


class TocList(BaseModel):
    model_config = ConfigDict(extra="ignore")
    toc: list[TocItem] = Field(default_factory=list)


# min_length=1: the empty list is the shortest schema-valid completion under
# guided decoding — gemma was collapsing to it, cascading to the "Document"
# fallback. Forces instructor into a ValidationError → repair round.
class TocListInitial(BaseModel):
    model_config = ConfigDict(extra="ignore")
    toc: list[TocItem] = Field(min_length=1)


class TitleAppearance(BaseModel):
    model_config = ConfigDict(extra="ignore")
    answer: str = "no"

    @field_validator("answer", mode="before")
    @classmethod
    def _norm(cls, v):
        return _yn(v)


class TitleStartsSection(BaseModel):
    model_config = ConfigDict(extra="ignore")
    start_begin: str = "no"

    @field_validator("start_begin", mode="before")
    @classmethod
    def _norm(cls, v):
        return _yn(v)


class PhysicalIndexFix(BaseModel):
    model_config = ConfigDict(extra="ignore")
    physical_index: int | str | None = None


class SummaryVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")
    faithful: str = "no"
    missed_topics: list[str] = Field(default_factory=list)

    @field_validator("faithful", mode="before")
    @classmethod
    def _norm(cls, v):
        return _yn(v)


class AbstractResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    abstract: str | None = None


RESPONSE_SCHEMAS: dict[str, type[BaseModel]] = {
    "toc_detection": TocDetection,
    "page_index_present": PageIndexPresent,
    "toc_completion": TocCompletion,
    "toc_list": TocList,
    "toc_list_initial": TocListInitial,
    "title_appearance": TitleAppearance,
    "title_starts_section": TitleStartsSection,
    "physical_index_fix": PhysicalIndexFix,
    "summary_verdict": SummaryVerdict,
    "abstract": AbstractResult,
}


def resolve_schema(key: str) -> type[BaseModel]:
    try:
        return RESPONSE_SCHEMAS[key]
    except KeyError as e:
        raise KeyError(
            f"unknown response schema {key!r}; "
            f"registered: {sorted(RESPONSE_SCHEMAS)}"
        ) from e
