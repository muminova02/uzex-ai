// Typed client for the FastAPI layer. Money/precise ratios arrive as exact decimal strings.
export type Money = { exact: string; approx_bn: number } | null;
export type Ratio = { exact: string; num: string; den: string } | null;

export interface Meta {
  mode: "production" | "demo" | "no_llm";
  dataset: { coverage_start: string; coverage_end: string; as_of: string; synthetic: boolean; fingerprint: string };
  assistant: { configured: boolean; provider: string | null; model: string | null; cooldown_s: number; limit_kind: string | null };
  budget_s: number;
}
export interface Option { key: string; label?: string; uz?: string; ru?: string; deals?: number; unit?: string; includes_cancelled?: boolean }
export interface Options {
  commodities: Option[]; variants: Record<string, Option[]>; regions: Option[]; forms: Option[]; policies: Option[];
  currencies: string[]; statuses: Option[];
  dataset: Meta["dataset"];
}
export interface Envelope {
  dataset: Meta["dataset"]; applied_filters: Record<string, string | null>;
  status_policy: { name: string; statuses: string[]; includes_cancelled: boolean; label_ru: string; label_uz: string };
  empty?: boolean;
}
export interface Overview extends Envelope {
  deals: number; value_uzs: Money; distinct_sellers: number; distinct_buyers: number; usd_converted_deals: number;
  cancelled: { count: number; denominator: number; share_pct: Ratio };
}
export interface TrendMonth { month: string; deals: number; value_uzs: Money; usd_converted: number; partial: boolean; covered_from: string; covered_to: string }
export interface Trend extends Envelope { months: TrendMonth[] }
export interface RankItem { key: string; label: string; label_uz?: string; label_ru?: string; value_uzs: Money; deals: number }
export interface Rank extends Envelope { dim: string; items: RankItem[]; top_n: number; total_groups: number; truncated: boolean }
export interface StatusItem { status_code: string; status: string; label_ru: string; label_uz: string; deals: number; share_pct: Ratio }
export interface StatusDist extends Envelope { items: StatusItem[]; denominator: number }
export interface PricePoint { month: string; currency: string; unit: string; deals: number; weighted_price: Ratio; approx: number | null }
export interface Price extends Envelope { available: boolean; reason?: string; variant_label?: string; unit?: string; currencies?: string[]; series: PricePoint[]; method?: string }
export interface DealRow { [k: string]: string | number | null }
export interface Deals extends Envelope { rows: DealRow[]; total: number; page: number; size: number; pages: number; sort: string; dir: string }
export interface DealDetail { deal: Record<string, string | number | boolean | null>; evidence: { source_file: string; source_row: number; query: string; params: string[] } }
export interface AnswerTable { caption: string | null; columns: { label: string; num: boolean }[]; rows: string[][]; note: string | null }
export interface AnswerBlocks { heading: string; tables: AnswerTable[]; notes: string[] }
export interface ChatReply {
  kind: string; text: string; options: { index: number; label: string }[]; sid: string | null; evidence_id: string | null;
  status: string | null; reason?: string | null; lang?: string | null; question_sent?: string; timings_ms?: Record<string, number> | null;
  blocks?: AnswerBlocks | null; banner?: string | null;
}
export interface Evidence {
  query_id: string; status: string; question: string | null; source_files: { file: string; table: string; sha256: string; rows: number }[];
  source_row_count: number | null; period: Record<string, unknown> | null; filters: { field: string; op: string; value: unknown; sql: string }[];
  entities: { kind: string; text: string; status: string; method: string; ids: string[] }[]; status_policy: Record<string, string[]>;
  currency_unit: Record<string, unknown>; sample_ids: string[]; sample_note: string; truncation: Record<string, unknown> | null;
  notes: string[]; as_of: string; llm: Record<string, unknown>; queries: { purpose: string; sql: string; params: unknown[]; rows: number | null; ms: number | null }[];
}

export class ApiError extends Error {
  constructor(public status: number, public body: { error?: string; message?: string; field?: string; total?: number; limit?: number }) {
    super(body?.message || body?.error || `HTTP ${status}`);
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin", ...init, headers: { Accept: "application/json", ...(init?.headers || {}) } });
  let body: unknown = null;
  try { body = await r.json(); } catch { /* non-JSON */ }
  if (!r.ok) throw new ApiError(r.status, (body as ApiError["body"]) || {});
  return body as T;
}

export const post = <T,>(path: string, data: unknown, signal?: AbortSignal) =>
  api<T>(path, { method: "POST", body: JSON.stringify(data), headers: { "Content-Type": "application/json" }, signal });
