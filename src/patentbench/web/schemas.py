"""Pydantic request models for the Patent Workbench API."""
from __future__ import annotations

from pydantic import BaseModel, Field


class TabCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class TabPatch(BaseModel):
    name: str | None = Field(default=None, max_length=120)


class DocumentsAdd(BaseModel):
    text: str | None = None            # free text / pasted numbers / URLs
    numbers: list[str] | None = None   # pre-confirmed canonical numbers (upload flow)
    source: str = "manual"
    reading_model: str | None = None   # model for the full-text digest; None = cheapest


class DocumentNumberEdit(BaseModel):
    number: str = Field(min_length=4, max_length=40)


class BenchmarkSet(BaseModel):
    text: str = Field(min_length=1, max_length=2000)   # a number or a link containing one


class NotebookConfig(BaseModel):
    notebook_id: str | None = None     # None/empty disconnects
    notebook_title: str | None = None
    source_ids: list[str] = []
    auto_add: bool = False             # mirror new candidates into the notebook


class NotebookCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)


# A pasted patent excerpt / long instruction is a legitimate question — cap it
# generously (the prompt builder clips per-turn history downstream) rather than
# rejecting a long message with an opaque 422.
MAX_QUESTION = 500_000


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION)
    model: str | None = None
    skills: list[str] = []
    use_documents: bool = True
    ask_notebook: bool = False


class AskNotebookRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION)


class DeepCompareRequest(BaseModel):
    model: str | None = None           # answer/compile model (chat dropdown)
    skills: list[str] = []
    question: str | None = Field(default=None, max_length=MAX_QUESTION)
    doc_ids: list[int] | None = None   # subset to analyze; None/empty = all candidates
    reading_model: str | None = None   # model that reads full texts; None = cheapest


class LessonCreate(BaseModel):
    skill: str
    lesson: str = Field(min_length=1, max_length=8000)
