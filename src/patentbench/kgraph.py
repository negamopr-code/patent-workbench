"""Knowledge-graph classification: place a benchmark/document feature into the
cross-tab taxonomy field › block › function › option, and propose the interrelated
blocks it involves.

Pure LLM glue — the persistence (kg_node/kg_edge/kg_feature) lives in db.py. A cheap
model is enough: this is a labelling task, not a ranking one. Tests monkeypatch
`classify_feature` so no `claude` process is spawned.
"""
from __future__ import annotations

import json
import os
import re

from . import claude_bridge, db

# Labelling is cheap and high-volume (every feature, and a whole-corpus rebuild), so
# it defaults to the extract/haiku model. Override with PB_KG_MODEL.
KG_MODEL = os.environ.get("PB_KG_MODEL", claude_bridge.EXTRACT_MODEL)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

_PROMPT = (
    "You organise patent technical features into a shared knowledge graph so the same "
    "concept, described in different patents, lands on the same node.\n\n"
    "The graph is a 4-level hierarchy:\n"
    "  FIELD    — broad technical domain (e.g. 'Aerosol devices', 'Battery management')\n"
    "  BLOCK    — a subsystem / component within the field (e.g. 'Battery', 'Heater', 'MCU')\n"
    "  FUNCTION — what that block does (e.g. 'Temperature measurement of the battery')\n"
    "  OPTION   — the concrete way it is done, the specific variant (e.g. "
    "'Thermistor in a voltage divider', 'Direct thermistor resistance reading')\n\n"
    "Plus RELATED BLOCKS: other blocks this feature involves / interacts with "
    "(e.g. a battery temperature-measurement option involves 'MCU' and 'Battery gauge').\n\n"
    "REUSE existing nodes whenever the feature fits one — do NOT invent a near-duplicate "
    "with slightly different wording. Existing nodes are listed below with their ids; if "
    "you reuse the exact option, put its id in \"matched_option_id\".\n\n"
    "{tree}\n\n"
    "Return STRICT JSON only, no prose:\n"
    "{{\n"
    '  "field": "...",\n'
    '  "block": "...",\n'
    '  "function": "...",\n'
    '  "option": "...",\n'
    '  "related_blocks": ["...", "..."],\n'
    '  "matched_option_id": null,\n'
    '  "confidence": 0.0\n'
    "}}\n"
    "Keep each label short (2-6 words), Title Case, English. related_blocks may be empty.\n\n"
    "FEATURE TO CLASSIFY:\n"
)


def _tree_summary(max_nodes: int = 300) -> str:
    """A compact, id-tagged listing of existing option/function nodes with their
    field›block path, so the model reuses them instead of duplicating."""
    tree = db.kg_tree()
    lines: list[str] = []

    def walk(node, trail):
        path = trail + [node["name"]]
        if node["kind"] in ("option", "function") and len(lines) < max_nodes:
            lines.append(f"  [{node['id']}] ({node['kind']}) " + " › ".join(path))
        for ch in node.get("children", []):
            walk(ch, path)

    for root in tree["nodes"]:
        walk(root, [])
    if not lines:
        return "EXISTING NODES: (none yet — you are seeding the graph)"
    return "EXISTING NODES (reuse by id where the feature fits):\n" + "\n".join(lines)


def _parse(text: str) -> dict | None:
    m = _JSON_RE.search(text or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    out = {
        "field": (obj.get("field") or "").strip(),
        "block": (obj.get("block") or "").strip(),
        "function": (obj.get("function") or "").strip(),
        "option": (obj.get("option") or "").strip(),
        "related_blocks": [str(x).strip() for x in (obj.get("related_blocks") or [])
                           if str(x).strip()][:8],
        "matched_option_id": obj.get("matched_option_id"),
    }
    try:
        out["confidence"] = float(obj.get("confidence") or 0)
    except (ValueError, TypeError):
        out["confidence"] = 0.0
    if not out["field"] and not out["option"]:
        return None
    return out


def classify_feature(name: str, model: str | None = None) -> dict:
    """Classify one feature string into field/block/function/option (+ related blocks).
    Returns the parsed classification, or {'error': ...}."""
    name = (name or "").strip()
    if not name:
        return {"error": "empty feature"}
    prompt = _PROMPT.replace("{tree}", _tree_summary()) + name
    res = claude_bridge._run_claude(prompt, model or KG_MODEL)
    if "error" in res:
        return res
    parsed = _parse(res["answer"])
    if not parsed:
        return {"error": "could not parse classification", "raw": res["answer"][:400]}
    return parsed


def apply_classification(cls: dict, feature_name: str, tab_id: int | None = None,
                         doc_id: int | None = None, status: str | None = None,
                         note: str | None = None) -> dict:
    """Persist a classification: get-or-create the field›block›function›option chain,
    attach the feature occurrence to the deepest node, and wire ⇄ related-block edges
    from the deepest node to each named related block (created under the same field).
    Returns the node path for UI display."""
    node_id = None
    matched = cls.get("matched_option_id")
    if matched and db.kg_path(matched):     # LLM reused an existing node by id
        node_id = matched
    else:
        ids = db.kg_ensure_path(cls.get("field", ""), cls.get("block", ""),
                                cls.get("function", ""), cls.get("option", ""))
        node_id = ids.get("node_id")
    if not node_id:
        return {"error": "classification had no usable node"}
    db.kg_attach_feature(node_id, feature_name, tab_id=tab_id, doc_id=doc_id,
                         status=status, note=note)
    # relate the deepest node to sibling blocks it involves (MCU, gauge…)
    field_name = cls.get("field", "")
    for rb in cls.get("related_blocks", []):
        if not field_name:
            break
        ids = db.kg_ensure_path(field_name, rb)
        rb_id = ids.get("node_id")
        if rb_id and rb_id != node_id:
            db.kg_add_edge(node_id, rb_id, "involves")
    return {"node_id": node_id, "path": db.kg_path(node_id)}
