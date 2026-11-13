import type { AnswerBlocks } from "../api";

/** Structured answer: heading, real semantic tables (row cards on narrow screens via CSS + data-label), notes.
 *  Every string comes from the backend renderer (already localized, grounded and formatted); nothing is computed here. */
export default function AnswerView({ blocks, banner }: { blocks: AnswerBlocks; banner?: string | null }) {
  return (
    <div className="answer">
      {banner && <p className="answer-banner">{banner}</p>}
      {blocks.heading && <p className="answer-head">{blocks.heading}</p>}
      {blocks.tables.map((t, ti) => (
        <div className="answer-table" key={ti}>
          <table>
            {t.caption && <caption>{t.caption}</caption>}
            <thead><tr>{t.columns.map((c, i) => <th key={i} scope="col" className={c.num ? "num" : undefined}>{c.label}</th>)}</tr></thead>
            <tbody>
              {t.rows.map((r, ri) => (
                <tr key={ri}>
                  {r.map((cell, ci) => ci === 0
                    ? <th key={ci} scope="row" data-label={t.columns[0]?.label}>{cell}</th>
                    : <td key={ci} className={t.columns[ci]?.num ? "num" : undefined} data-label={t.columns[ci]?.label}><span className="v">{cell}</span></td>)}
                </tr>
              ))}
            </tbody>
          </table>
          {t.note && <p className="answer-note">{t.note}</p>}
        </div>
      ))}
      {blocks.notes.map((n, i) => <p key={i} className="answer-meta">{n}</p>)}
    </div>
  );
}
