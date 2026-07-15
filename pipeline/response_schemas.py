"""Pydantic schemas for tree-building LLM structured outputs.

Named entries in `RESPONSE_SCHEMAS` cross the Temporal activity boundary as a
string key — Pydantic classes can't serialize as workflow input, so the workflow
passes the key and the activity resolves it. `extra="allow"` on TocItem lets
downstream code annotate items (appear_start, list_index, etc.) without churn.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class TocItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    title: str = ""
    structure: str | None = None
    physical_index: int | str | None = None
    page: int | str | None = None


class TocList(BaseModel):
    model_config = ConfigDict(extra="ignore")
    toc: list[TocItem] = Field(default_factory=list)


# Initial TOC generation must produce ≥1 item — an empty list is the shortest
# schema-valid completion under vLLM guided decoding, and gemma was collapsing
# to it, cascading to the "Document" fallback. min_length=1 forces instructor
# to see a ValidationError and retry with a repair prompt.
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
