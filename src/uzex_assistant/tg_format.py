"""Telegram HTML formatting: escaping, safe splitting (≤ 4096 chars), evidence view. No LLM calls.

Every chunk is built from whole "units" (one line, or one <pre> block). Each unit is escaped first
and wrapped in balanced tags, so a split can never cut a tag or an HTML entity.
"""
from __future__ import annotations

import html

TG_LIMIT = 4096
SAFE_LIMIT = 3900
HEADERS = {"Условия:", "Метод расчёта:", "Shartlar:", "Hisoblash usuli:", "Шартлар:", "Ҳисоблаш усули:"}
SOURCE_PREFIX = ("Источник:", "Manba:", "Манба:")


def esc(s: str) -> str:
    return html.escape(str(s), quote=False)


def _cut_raw(line: str, limit: int) -> list[str]:
    """Split one raw line so that each escaped piece fits ``limit`` (prefer spaces)."""
    out, cur = [], ""
    for word in line.split(" "):
        cand = (cur + " " + word) if cur else word
        if len(esc(cand)) <= limit:
            cur = cand
            continue
        if cur:
            out.append(cur)
        while len(esc(word)) > limit:  # a single very long token
            n = limit
            while len(esc(word[:n])) > limit:
                n -= 1
            out.append(word[:n])
            word = word[n:]
        cur = word
    if cur:
        out.append(cur)
    return out


def split_units(units: list[str], limit: int = SAFE_LIMIT) -> list[str]:
    chunks, cur = [], ""
    for u in units:
        if len(u) > limit:  # should not happen: units are pre-cut
            u = u[:limit]
        if cur and len(cur) + 1 + len(u) > limit:
            chunks.append(cur)
            cur = u
        else:
            cur = (cur + "\n" + u) if cur else u
    if cur:
        chunks.append(cur)
    return chunks or [""]


def answer_html(text: str, banner: str | None = None, limit: int = SAFE_LIMIT) -> list[str]:
    units: list[str] = []
    if banner:
        units.append(f"<b>{esc(banner)}</b>")
    first = True
    for raw in text.split("\n"):
        pieces = _cut_raw(raw, limit - 20) if raw else [""]
        for piece in pieces:
            e = esc(piece)
            if first and piece.strip():
                units.append(f"<b>{e}</b>")
                first = False
            elif piece.strip() in HEADERS:
                units.append(f"<b>{e}</b>")
            elif piece.startswith(SOURCE_PREFIX):
                units.append(f"<i>{e}</i>")
            else:
                units.append(e)
    return split_units(units, limit)


def _pre(s: str, max_escaped: int = 3000) -> str:
    """<pre> block whose escaped content never exceeds ``max_escaped`` (cut on the raw text, so no
    entity is split)."""
    if len(esc(s)) > max_escaped:
        n = len(s)
        while n > 0 and len(esc(s[:n])) > max_escaped - 2:
            n = int(n * 0.9)
        s = s[:n] + " …"
    return f"<pre>{esc(s)}</pre>"


def evidence_html(res, limit: int = SAFE_LIMIT) -> list[str]:
    """Full evidence for the 'Manba / Источник' button (data from the stored AnswerResult only)."""
    ev = res.evidence
    U = [f"<b>Manba / Источник</b> · <code>{esc(ev.query_id)}</code>"]
    if res.question:
        U.append(f"❓ {esc(res.question)}")
    U.append(f"status: <code>{esc(res.status)}</code>" + (f" ({esc(res.reason_code)})" if res.reason_code else ""))
    if ev.source_files:
        U.append("files: " + esc(", ".join(f"{f['file']} ({f['rows']} rows, sha256 {f['sha256']}…)" for f in ev.source_files)))
    if ev.source_row_count is not None:
        U.append(f"source rows: <b>{esc(ev.source_row_count)}</b>")
    if ev.period_effective:
        U.append("period: " + esc(f"{ev.period_effective.get('start')} … {ev.period_effective.get('end')}"))
    if ev.compare_periods:
        U.append("compare: " + esc(", ".join(f"{k}: {v['effective'][0]}…{v['effective'][1]}" for k, v in ev.compare_periods.items())))
    for f in ev.filters:
        U.append("filter: " + esc(f"{f['field']} {f['op']} {f['value']}  [{f['sql']}]"))
    for n in ev.normalization:
        U.append("entity: " + esc(f"{n['kind']} '{n['text']}' → {n['status']} ({n['method']}) ids={n['ids']}"))
    for k, codes in ev.status_policy.items():
        U.append("status policy " + esc(f"{k}: {', '.join(codes)}"))
    if ev.currency_unit:
        U.append("currency: " + esc(str(ev.currency_unit)))
    if ev.truncation:
        U.append("truncation: " + esc(str(ev.truncation)))
    if ev.sample_ids:
        U.append("sample deal ids: " + esc(", ".join(ev.sample_ids)))
    if ev.notes:
        U.append("notes: " + esc("; ".join(ev.notes)))
    for q in ev.queries:
        U.append(esc(f"q{q.query_index} [{q.purpose}] rows={q.row_count} {q.elapsed_ms} ms" + (f" ERROR {q.error}" if q.error else "")))
        U.append(_pre(q.sql))
        if q.params:
            U.append("params: " + esc(str(q.params)[:1500]))
    llm = res.llm or {}
    if llm.get("limit") or llm.get("last_wait_hint_s"):
        U.append("provider limit: " + esc(f"{llm.get('limit')} wait_s={llm.get('last_wait_hint_s')} gated={llm.get('gated', False)}"))
    if llm.get("served_by") or llm.get("model"):
        U.append("model: " + esc(f"{llm.get('provider', '')} {llm.get('served_by') or llm.get('model')}"))
    U.append(esc(f"as_of: {ev.as_of}; prompt: {ev.prompt_version}"))
    # cut over-long text units
    units = []
    for u in U:
        if len(u) <= limit:
            units.append(u)
        else:
            units += [esc(p) for p in _cut_raw(html.unescape(u.replace("<b>", "").replace("</b>", "")), limit - 20)]
    return split_units(units, limit)


def rich_message(blocks: dict, banner: str | None = None):
    """Telegram rich message (sendRichMessage) with real table blocks, built from the SAME renderer blocks as the web
    table and the plain-text answer. Returns an aiogram InputRichMessage; the caller falls back to answer_html on error."""
    from aiogram.types import (InputRichBlockParagraph, InputRichBlockSectionHeading, InputRichBlockTable, InputRichMessage,
                               RichBlockTableCell)
    out = []
    if banner:
        out.append(InputRichBlockParagraph(text=banner))
    if blocks.get("heading"):
        out.append(InputRichBlockSectionHeading(text=blocks["heading"], size=3))
    for t in blocks.get("tables", []):
        head = [RichBlockTableCell(text=c["label"], is_header=True, align="right" if c["num"] else "left", valign="top")
                for c in t["columns"]]
        body = [[RichBlockTableCell(text=str(v), align="right" if (i < len(t["columns"]) and t["columns"][i]["num"]) else "left",
                                    valign="top") for i, v in enumerate(r)] for r in t["rows"]]
        out.append(InputRichBlockTable(cells=[head] + body, is_bordered=True, is_striped=True, caption=t.get("caption")))
        if t.get("note"):
            out.append(InputRichBlockParagraph(text=t["note"]))
    for n in blocks.get("notes", []):
        out.append(InputRichBlockParagraph(text=n))
    return InputRichMessage(blocks=out)
