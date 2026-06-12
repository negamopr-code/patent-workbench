"""Skill self-update: append durable lessons to a skill's references/lessons.md.

The container mounts the host ~/.claude/skills READ-WRITE at SKILLS_RW_DIR
(user-confirmed choice — lessons become visible to every project immediately).
Writes are flock-guarded and append-only; the skill name is validated against
the real skills listing so no path traversal is possible.
"""
from __future__ import annotations

import datetime
import fcntl
import os

from . import claude_bridge

SKILLS_RW_DIR = os.environ.get("SKILLS_RW_DIR", "/skills-rw")


def available() -> tuple[bool, str]:
    if not os.path.isdir(SKILLS_RW_DIR):
        return False, (f"writable skills dir not mounted ({SKILLS_RW_DIR}) — "
                       "lessons cannot be auto-appended")
    return True, ""


def append_lesson(skill: str, lesson: str, source: str = "patent-workbench") -> dict:
    """Append one dated lesson. Returns {ok, path} | {error}."""
    ok, why = available()
    if not ok:
        return {"error": why}
    if skill not in {s["name"] for s in claude_bridge.list_skills()}:
        return {"error": f"unknown skill: {skill}"}
    lesson = (lesson or "").strip()
    if not lesson:
        return {"error": "empty lesson"}
    ref_dir = os.path.join(SKILLS_RW_DIR, skill, "references")
    path = os.path.join(ref_dir, "lessons.md")
    date = datetime.date.today().isoformat()
    entry = f"\n## {date} — from {source}\n\n{lesson}\n"
    try:
        os.makedirs(ref_dir, exist_ok=True)
        new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as fh:
            fcntl.lockf(fh, fcntl.LOCK_EX)
            if new:
                fh.write(f"# Lessons — /{skill}\n\nAppended automatically by tools "
                         "that use this skill. Newest at the bottom.\n")
            fh.write(entry)
    except OSError as exc:
        return {"error": f"could not write lesson: {exc}"}
    return {"ok": True, "path": path}
