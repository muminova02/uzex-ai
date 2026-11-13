import type { ReactNode } from "react";
import { useApp } from "../App";
import { fmtDate } from "../format";
import { t } from "../i18n";
import Icon from "./Icon";

/** Page title + dataset period and status snapshot date (from /api/meta, never hardcoded) + optional actions. */
export default function PageHeader({ title, lead, actions }: { title: string; lead?: string; actions?: ReactNode }) {
  const { lang, meta } = useApp();
  const ds = meta?.dataset;
  return (
    <header className="page-head">
      <div>
        <h1>{title}</h1>
        {lead && <p className="lead">{lead}</p>}
        {ds && (
          <div className="meta">
            <span className="item"><Icon name="calendar" /><span className="sr-only">{t("period", lang)}: </span>
              <span className="tnum">{fmtDate(ds.coverage_start)} — {fmtDate(ds.coverage_end)}</span></span>
            <span className="sep" aria-hidden="true" />
            <span className="item">{t("status_on", lang)}: <span className="tnum">{fmtDate(ds.as_of)}</span></span>
          </div>
        )}
      </div>
      {actions && <div className="head-actions">{actions}</div>}
    </header>
  );
}
