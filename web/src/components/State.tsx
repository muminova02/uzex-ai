import { t, type Lang } from "../i18n";
import Icon from "./Icon";

/** Distinct loading / failure / verified-empty states (a failure is never shown as "no data"). */
export default function State({ loading, error, empty, reload, lang, rows = 3 }: {
  loading: boolean; error: Error | null; empty?: boolean; reload: () => void; lang: Lang; rows?: number;
}) {
  if (loading) return (
    <div className="state" aria-busy="true" role="status">
      <span className="sr-only">{t("loading", lang)}</span>
      <div style={{ width: "100%", display: "grid", gap: 10 }} aria-hidden="true">
        {Array.from({ length: rows }).map((_, i) => <div key={i} className="skeleton" style={{ height: 18, width: `${90 - i * 12}%` }} />)}
      </div>
    </div>
  );
  if (error) return (
    <div className="state error" role="alert">
      <span className="badge-ic"><Icon name="alert" /></span>
      <span>{t("error", lang)}: {error.message}</span>
      <button className="btn sm" onClick={reload}>{t("retry", lang)}</button>
    </div>
  );
  if (empty) return (
    <div className="state empty" role="status"><span className="badge-ic"><Icon name="search" /></span>{t("empty", lang)}</div>
  );
  return null;
}
