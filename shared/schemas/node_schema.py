from typing import Optional
from pydantic import BaseModel, Field


class BoundingBox(BaseModel):
    """Normalized coordinates [0,1] relative to page dimensions."""
    x0: float
    y0: float
    x1: float
    y1: float


class VisualElement(BaseModel):
    element_id: str
    element_type: str  # "figure" | "table" | "isolate_formula"
    page_index: int    # 1-based physical page number
    bbox: BoundingBox
    asset_uri: str     # s3://… or local path; routed through shared.s3_io
    caption: Optional[str] = None
    ocr_text: Optional[str] = None
    ocr_parsed: Optional[dict] = None  # normalized chandra output (see chandra_parser.py)
    chem_entities: list[str] = Field(default_factory=list)
    structured_data: Optional[str] = None  # markdown table for tables


class NodeSource(BaseModel):
    pdf_path: str
    paper_id: str
    page_image_uris: list[str] = Field(default_factory=list)  # s3://… or local; same contract as VisualElement.asset_uri


class TreeNode(BaseModel):
    model_config = {"frozen": False}

    title: str
    node_id: str
    start_index: int  # 1-based physical page (inclusive)
    end_index: int    # 1-based physical page (inclusive)
    summary: Optional[str] = None
    nodes: list["TreeNode"] = Field(default_factory=list)
    source: Optional[NodeSource] = None
    visual_elements: list[VisualElement] = Field(default_factory=list)


class DocumentTree(BaseModel):
    paper_id: str
    pdf_path: str
    total_pages: int
    doc_description: Optional[str] = None
    root_nodes: list[TreeNode]
