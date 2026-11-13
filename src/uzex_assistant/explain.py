"""Deterministic explanation of a stored, verified AnswerResult (no LLM call).

Each statement restates a field of that result (value, unit, status policy, period, filters, row count)
and its documented meaning. The only new numbers are simple arithmetic on result fields (a difference,
or delivered ÷ purchased × 100), each shown with its formula. The grounding check receives them as
derived values. The explanation never says anything about causes, forecasts, profitability or the
market: the data cannot support such claims.
"""
from __future__ import annotations

from decimal import Decimal

from .lang import Lang, latn_to_cyrl
from .numbers import exact_ratio
from .plan import METRICS
from .render import POLICY, STATUS, Rendered, Renderer, V, _l, _strip_marks, fmt_num, grounding_errors
from .schema import STATUS_POLICIES

MEANING = {
    "count": ("число сделок, попавших под условия", "shartlarga mos bitimlar soni"),
    "sum_money": ("суммарная стоимость сделок (стоимость контрактов, не прибыль)", "bitimlarning umumiy qiymati (shartnomalar qiymati, foyda emas)"),
    "sum_qty": ("суммарное количество товара в единицах контракта", "shartnoma birligidagi umumiy tovar miqdori"),
    "weighted": ("средняя цена с учётом объёма каждой сделки: Σ сумм ÷ Σ количества", "har bir bitim hajmini hisobga olgan o'rtacha narx: Σ summa ÷ Σ miqdor"),
    "mean": ("простое среднее цен отдельных сделок, без учёта их объёма", "bitimlar hajmini hisobga olmagan oddiy o'rtacha narx"),
    "share": ("доля аннулированных (неоплаченных) сделок среди всех сделок в выборке", "tanlovdagi barcha bitimlar ichida bekor qilingan (to'lanmagan) bitimlar ulushi"),
    "minmax": ("крайнее значение цены за единицу среди сделок", "bitimlar orasida birlik narxining chekka qiymati"),
    "distinct": ("число различных участников/вариантов", "turli ishtirokchilar/variantlar soni"),
    "count_if": ("число сделок с указанным признаком на 30.10.2026", "30.10.2026 holatiga ko'ra ko'rsatilgan belgili bitimlar soni"),
}
STATUS_MEANING = {
    "new": ("заключена в последний день данных", "ma'lumotlarning oxirgi kunida tuzilgan"),
    "unsigned": ("договор ещё не подписан", "shartnoma hali imzolanmagan"),
    "concluded": ("форвард/фьючерс подписан, срок оплаты не истёк", "forvard/fyuchers imzolangan, to'lov muddati o'tmagan"),
    "awaiting_payment": ("спот подписан, срок оплаты не истёк", "spot imzolangan, to'lov muddati o'tmagan"),
    "paid": ("оплачена, товар ещё не поставлен", "to'langan, tovar hali yetkazilmagan"),
    "partially_fulfilled": ("оплачена, поставлена только часть товара", "to'langan, tovarning faqat bir qismi yetkazilgan"),
    "fulfilled": ("оплачена и поставлена полностью", "to'langan va to'liq yetkazilgan"),
    "cancelled_unpaid": ("не оплачена в срок и аннулирована — сделка не состоялась", "muddatida to'lanmagan va bekor qilingan — bitim amalga oshmagan"),
}
CAVEAT = ("Пояснение описывает только этот результат. Причины, прогнозы, прибыльность и выводы о рынке из этих данных не следуют.",
          "Izoh faqat shu natijani tavsiflaydi. Sabablar, prognozlar, foydalilik va bozor haqidagi xulosalar bu ma'lumotlardan kelib chiqmaydi.")


def _kind(m: str) -> str:
    spec = METRICS[m]
    if spec.kind == "ratio":
        return {"cancellation_share": "share", "mean_unit_price": "mean", "avg_deal_value_uzs": "mean"}.get(m, "weighted")
    if spec.kind == "sum":
        return "sum_qty" if spec.unit == "native_unit" else "sum_money"
    return {"count": "count", "count_if": "count_if", "distinct": "distinct", "min": "minmax", "max": "minmax"}[spec.kind]


class Explainer(Renderer):
    def explain(self, res, lang: Lang) -> Rendered:
        self.lang = lang.lang
        self.catalog_strings = []
        self.derived: list[str] = []
        ru = self.lang == "ru"
        head = (f"Пояснение к предыдущему ответу (запрос {V(res.evidence.query_id)}):" if ru
                else f"Oldingi javobga izoh (so'rov {V(res.evidence.query_id)}):")
        if res.question:
            head += f"\n«{V(res.question)}»"
        body = self._body(res)
        cond = self._conditions(res, short=res.status != "success")
        text = "\n\n".join(x for x in (head, body, cond, _l(CAVEAT, self.lang)) if x)
        bad = grounding_errors(_strip_marks(text), res, self.catalog_strings + self.derived)
        out = latn_to_cyrl(text) if (lang.lang == "uz" and lang.script == "cyrl") else _strip_marks(text)
        return Rendered(lang.lang, lang.script, out, {}, not bad, bad, False)

    def _d(self, v) -> str:
        self.derived.append(str(v))
        return str(v)

    def _body(self, res) -> str:
        ru = self.lang == "ru"
        st = res.status
        if st != "success":
            return {
                "ambiguous": ("Предыдущий вопрос не получил числового ответа: требовалось уточнение. Объяснять пока нечего — выберите вариант или задайте вопрос точнее.",
                              "Oldingi savolga raqamli javob berilmagan: aniqlashtirish kerak edi. Hozircha izohlaydigan narsa yo'q — variantni tanlang yoki savolni aniqroq bering."),
                "no_matching_rows": ("Результат — отсутствие подходящих данных: по этим условиям сделок нет (это проверено), либо названный объект не найден в справочниках.",
                                     "Natija — mos ma'lumot yo'qligi: bu shartlarda bitimlar yo'q (tekshirilgan) yoki ko'rsatilgan obyekt ma'lumotnomalarda topilmagan."),
                "missing_field": ("Запрошенных сведений в наборе данных нет, поэтому вывод сделать нельзя.",
                                  "So'ralgan ma'lumot to'plamda yo'q, shuning uchun xulosa chiqarib bo'lmaydi."),
                "out_of_period": ("Запрошенный период вне данных (03.01.2022–30.10.2026), поэтому выводов о нём нет.",
                                  "So'ralgan davr ma'lumotlardan tashqarida (03.01.2022–30.10.2026), shuning uchun u haqda xulosa yo'q."),
                "unsupported": ("Предыдущий запрос не был выполнен, объяснять нечего.", "Oldingi so'rov bajarilmagan, izohlaydigan narsa yo'q."),
                "execution_error": ("Предыдущий запрос не выполнен из-за временного сбоя, результата нет. Это не означает отсутствие данных.",
                                    "Oldingi so'rov vaqtinchalik nosozlik tufayli bajarilmagan, natija yo'q. Bu ma'lumot yo'qligini anglatmaydi."),
            }[st][0 if ru else 1]
        if res.verified_zero:
            return ("Ноль проверен по базе: при этих условиях не найдено ни одной сделки. Это не ошибка и не отсутствие данных."
                    if ru else "Nol baza bo'yicha tekshirilgan: bu shartlarda birorta bitim topilmadi. Bu xato ham, ma'lumot yo'qligi ham emas.")
        if res.intent == "lookup":
            return self._lookup_expl(res)
        if res.intent == "list":
            return self._list_expl(res)
        if res.intent == "comparison":
            return self._cmp_expl(res)
        return self._agg_expl(res)

    def _metric_line(self, f) -> str:
        m = f.name.split("__")[0]
        pol = next((p for p, cs in STATUS_POLICIES.items() if res_policy(self.res_cache, m) == list(cs)), None)
        line = f"• {self.label(m)} = {self.val(f)} — {_l(MEANING[_kind(m)], self.lang)}"
        if f.rows_used is not None:
            line += (f"; учтено сделок: {V(fmt_num(f.rows_used))}" if self.lang == "ru" else f"; hisobga olingan bitimlar: {V(fmt_num(f.rows_used))}")
        if pol in POLICY:
            line += f" ({_l(POLICY[pol], self.lang)})"
        return line

    def _agg_expl(self, res) -> str:
        self.res_cache = res
        rows = self._rows(res)
        ru = self.lang == "ru"
        if not any(f.group for f in res.facts):
            return "\n".join(self._metric_line(f) for f in rows[0]) if rows else ""
        m0 = rows[0][0].name
        first, last = rows[0][0], rows[-1][0]
        L = [self._metric_line(first)]
        L[0] = (f"Первое место: {self.group_label(first.group)} — {self.val(first)}." if ru
                else f"Birinchi o'rin: {self.group_label(first.group)} — {self.val(first)}.")
        if len(rows) > 1 and first.value is not None and last.value is not None:
            diff = Decimal(str(first.value)) - Decimal(str(last.value))
            dtxt = V(self._d(fmt_num(diff, 2 if METRICS[m0].unit in ('UZS', 'native_currency', 'price', 'price_uzs') else None)))
            L.append((f"Последнее из показанных ({V(len(rows))}-е): {self.group_label(last.group)} — {self.val(last)}; "
                      f"разница между первым и последним показанным: {dtxt} (первое − последнее)." if ru else
                      f"Ko'rsatilganlarning oxirgisi ({V(len(rows))}-o'rin): {self.group_label(last.group)} — {self.val(last)}; "
                      f"birinchi va oxirgi ko'rsatilgan orasidagi farq: {dtxt} (birinchi − oxirgi)."))
        L.append((f"Показатель «{self.label(m0)}»: {_l(MEANING[_kind(m0)], self.lang)}." if ru
                  else f"«{self.label(m0)}» ko'rsatkichi: {_l(MEANING[_kind(m0)], self.lang)}."))
        tr = res.evidence.truncation or {}
        if tr.get("total_groups"):
            L.append(self.t("trunc", shown=tr.get("shown"), total=tr["total_groups"]))
        if any(METRICS[f.name.split('__')[0]].group_unit for f in res.facts):
            L.append(self.t("units_sep"))
        return "\n".join(L)

    def _cmp_expl(self, res) -> str:
        self.res_cache = res
        ru = self.lang == "ru"
        L = []
        for f in res.facts:
            if not f.name.endswith("__change"):
                continue
            m = f.name[:-8]
            pct = next((x for x in res.facts if x.name == f"{m}__change_pct" and x.group == f.group), None)
            d = Decimal(str(f.value))
            word = ("выросло" if d > 0 else "снизилось" if d < 0 else "не изменилось") if ru else \
                   ("oshgan" if d > 0 else "kamaygan" if d < 0 else "o'zgarmagan")
            absval = type(f)(**{**f.model_dump(), "name": m, "value": str(abs(d))})
            if d == 0:
                L.append(f"• {self.label(m)}: {word}.")
                continue
            self._d(str(abs(d)))
            L.append((f"• {self.label(m)} во втором периоде {word} на {self.val(absval)}" if ru
                      else f"• {self.label(m)} ikkinchi davrda {self.val(absval)}ga {word}")
                     + (f" ({V(fmt_num(pct.value, 2))}%)." if pct else "."))
        if "compare_periods_aligned_to_equivalent_span" in res.evidence.notes:
            L.append(self.t("aligned"))
        return "\n".join(L)

    def _list_expl(self, res) -> str:
        ru = self.lang == "ru"
        tbl = res.table or {}
        cols, rows = tbl.get("columns", []), tbl.get("rows", [])
        total = next((f.value for f in res.facts if f.name == "total_rows"), None)
        order = (res.plan.order.by if res.plan and res.plan.order else "deal_date")
        L = [(f"Всего подходящих сделок: {V(fmt_num(total))}; в ответе показаны первые {V(len(rows))} по полю «{V(order)}»."
              if ru else f"Jami mos bitimlar: {V(fmt_num(total))}; javobda «{V(order)}» bo'yicha birinchi {V(len(rows))} tasi ko'rsatilgan.")]
        if rows:
            ci = {c: i for i, c in enumerate(cols)}
            r = rows[0]
            L.append((f"Первая в списке: сделка №{V(r[ci['deal_id']])} от {V(r[ci['deal_date']][8:10] + '.' + r[ci['deal_date']][5:7] + '.' + r[ci['deal_date']][:4])}."
                      if ru else f"Ro'yxatda birinchi: №{V(r[ci['deal_id']])} bitim, {V(r[ci['deal_date']][8:10] + '.' + r[ci['deal_date']][5:7] + '.' + r[ci['deal_date']][:4])}."))
        return "\n".join(L)

    def _lookup_expl(self, res) -> str:
        ru = self.lang == "ru"
        fs = {f.name: f.value for f in res.facts}
        if "deal_id" in fs:
            code = fs.get("status_code")
            L = [(f"Статус сделки №{V(fs['deal_id'])}: {V(fs['status'])} — {_l(STATUS_MEANING.get(code, ('', '')), 'ru')}."
                  if ru else f"№{V(fs['deal_id'])} bitim holati: {_l(STATUS.get(code, ('', '')), 'uz')} — {_l(STATUS_MEANING.get(code, ('', '')), 'uz')}.")]
            q, dq = fs.get("quantity_native"), fs.get("delivered_quantity")
            if q and dq is not None:
                pct = exact_ratio(Decimal(dq) * 100, q, 2)
                self._d(pct)
                L.append((f"Поставлено {V(fmt_num(dq))} из {V(fmt_num(q))}{self.unit_text(fs.get('quantity_unit'))}, то есть {V(fmt_num(pct))}% (поставлено ÷ куплено × 100)."
                          if ru else f"{V(fmt_num(q))}{self.unit_text(fs.get('quantity_unit'))} dan {V(fmt_num(dq))} yetkazilgan, ya'ni {V(fmt_num(pct))}% (yetkazilgan ÷ sotib olingan × 100)."))
            if fs.get("is_delivery_overdue"):
                L.append("Срок поставки оставшейся части истёк до 30.10.2026; что произошло после этой даты, неизвестно."
                         if ru else "Qolgan qismni yetkazish muddati 30.10.2026 gacha o'tgan; bu sanadan keyin nima bo'lgani noma'lum.")
            L.append(("Цена за единицу = сумма сделки ÷ количество; сумма — стоимость контракта, а не прибыль."
                      if ru else "Birlik narxi = bitim summasi ÷ miqdor; summa — shartnoma qiymati, foyda emas."))
            return "\n".join(L)
        if "deal_count" in fs:
            return (f"За период данных у этой организации {V(fmt_num(fs['deal_count']))} сделок, из них не аннулированы {V(fmt_num(fs['not_cancelled_count']))}."
                    if ru else f"Ma'lumotlar davrida bu tashkilotda {V(fmt_num(fs['deal_count']))} ta bitim bor, ulardan {V(fmt_num(fs['not_cancelled_count']))} tasi bekor qilinmagan.")
        return ""


def res_policy(res, metric: str):
    return (res.evidence.status_policy or {}).get(metric)


def explain(res, lang: Lang, catalog=None) -> Rendered:
    e = Explainer(catalog)
    e.res_cache = res
    return e.explain(res, lang)
