# Patent Workbench

Multi-tab patent project app — NotebookLM-style, but the chat is **Claude**.

**Live:** http://localhost:8099/ (container `patent-bench`, `--restart unless-stopped`)

Each **tab** is a named project (create / rename / delete, unlimited):

- **Add documents** (left): type/paste patent numbers, Google Patents or Espacenet
  links, or any text containing them — or drop a **photo** of a printed document
  list / a **PDF** / a `.txt`. Numbers are extracted (photos via Claude haiku OCR,
  no NotebookLM quota), canonicalized (OCR-recovery rules from patent-wiki-analyzer)
  and confirmed by you before insert.
- **Documents** (middle): each number is fetched from Google Patents in the
  background (title / abstract / claims / description, PDF-pdftotext fallback) and
  stored in SQLite — the chat works on the full text **without** NotebookLM.
  Google Patents + Espacenet links on every row.
- **Chat** (right): Claude model dropdown (fable-5 default), combinable **skill**
  checkboxes (from `~/.claude/skills`, injected into the prompt), "use documents"
  toggle, full per-tab history persisted. Optionally connect the tab to a
  **NotebookLM notebook**, pick exact source documents (select all / none) and
  fan the question there too (`--source-ids`); Claude compiles the answers.
- **Skill self-update**: when a durable lesson surfaces, Claude emits a
  `LESSON[skill]:` trailer (or you press *Save as lesson*) and it is appended to
  the skill's `references/lessons.md` via the read-write skills mount.

## Stack

FastAPI + vanilla JS, SQLite (WAL) in the `patent-bench-data` volume, headless
`claude -p` (creds seeded from `/seed:ro`), `nlm` CLI (notebooklm-mcp-cli) with the
shared auth profile mount. Same proven shape as antimartingale-studio / yt2nlm-web.

## Deploy / rebuild

```sh
./scripts/serve.sh        # build + run on host port 8099 (works from host or dev container)
```

Mounts: `/root/.claude → /seed:ro` (creds + skills), `/root/.claude/skills → /skills-rw`
(lesson write-back), `/root/claude-sandbox/persistent/nlm-profile → ~/.notebooklm-mcp-cli`.

## Dev

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-web.txt pytest
PYTHONPATH=src .venv/bin/python -m pytest tests/   # 17 tests
```
