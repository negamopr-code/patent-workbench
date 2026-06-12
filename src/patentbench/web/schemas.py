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


class DocumentNumberEdit(BaseModel):
    number: str = Field(min_length=4, max_length=40)


class BenchmarkSet(BaseModel):
    text: str = Field(min_length=1, max_length=2000)   # a number or a link containing one


class NotebookConfig(BaseModel):
    notebook_id: str | None = None     # None/empty disconnects
    notebook_title: str | None = None
    source_ids: list[str] = []


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20_000)
    model: str | None = None
    skills: list[str] = []
    use_documents: bool = True
    ask_notebook: bool = False


class AskNotebookRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20_000)


class LessonCreate(BaseModel):
    skill: str
    lesson: str = Field(min_length=1, max_length=8000)
