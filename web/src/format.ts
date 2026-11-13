// Exact formatting from decimal strings (no float arithmetic on money). Rounding happens only here,
// at the presentation boundary, half-up on the decimal string.
const NB = " ";

export function roundStr(exact: string, places: number): string {
  const neg = exact.startsWith("-");
  const s = neg ? exact.slice(1) : exact;
  const [ip, fp = ""] = s.split(".");
  if (fp.length <= places) return (neg ? "-" : "") + ip + (places ? "." + fp.padEnd(places, "0") : "");
  const keep = (ip + fp.slice(0, places)).split("").map(Number);
  if (Number(fp[places]) >= 5) {
    let i = keep.length - 1;
    while (i >= 0) { if (keep[i] === 9) { keep[i] = 0; i--; } else { keep[i]++; break; } }
    if (i < 0) keep.unshift(1);
  }
  const digits = keep.join("");
  const intLen = digits.length - places;
  return (neg ? "-" : "") + digits.slice(0, intLen) + (places ? "." + digits.slice(intLen) : "");
}

export function group(intPart: string): string {
  return intPart.replace(/\B(?=(\d{3})+(?!\d))/g, NB);
}

export function fmtExact(exact: string | null | undefined, places = 2): string {
  if (exact == null) return "—";
  const r = roundStr(exact, places);
  const [ip, fp] = r.split(".");
  return group(ip) + (fp ? "," + fp : "");
}

export function fmtInt(n: number | null | undefined): string {
  return n == null ? "—" : group(String(n));
}

/** Large UZS amounts as "220 477,16 mlrd so'm"; exact value is kept for titles/tooltips. */
export function fmtBn(exact: string | null | undefined, lang: "uz" | "ru"): string {
  if (exact == null) return "—";
  const neg = exact.startsWith("-");
  const s = neg ? exact.slice(1) : exact;
  const [ip, fp = ""] = s.split(".");
  const padded = ip.padStart(10, "0");
  const bn = padded.slice(0, -9).replace(/^0+(?=\d)/, "") + "." + padded.slice(-9) + fp;
  return (neg ? "-" : "") + fmtExact(bn, 2) + NB + (lang === "ru" ? "млрд сум" : "mlrd so'm");
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const [y, m, d] = iso.slice(0, 10).split("-");
  return `${d}.${m}.${y}`;
}

export const UNIT: Record<string, [string, string]> = {
  "тонна": ["t", "т"], "килограмм": ["kg", "кг"], "литр": ["litr", "л"], "штука": ["dona", "шт."],
  "куб.метр": ["m³", "м³"], "кв.метр": ["m²", "м²"], "метр": ["metr", "м"],
};
export const unitLabel = (u: string | null | undefined, lang: "uz" | "ru") => (u ? (UNIT[u]?.[lang === "uz" ? 0 : 1] ?? u) : "");
