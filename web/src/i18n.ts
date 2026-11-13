export type Lang = "uz" | "ru";

const D = {
  title: ["UZEX Data Assistant", "UZEX Data Assistant"],
  prototype: ["Hakaton prototipi · rasmiy UZEX xizmati emas", "Прототип для хакатона · не официальный сервис UZEX"],
  synthetic: ["Sintetik ma'lumotlar", "Синтетические данные"],
  period: ["Davr", "Период"],
  snapshot: ["Holatlar sanasi", "Статусы на"],
  nav_dashboard: ["Dashboard", "Дашборд"], nav_assistant: ["AI yordamchi", "AI-ассистент"], nav_explorer: ["Bitimlar", "Сделки"],
  lang_switch: ["Русский", "O'zbekcha"],
  // filters
  filters: ["Filtrlar", "Фильтры"], reset: ["Tozalash", "Сбросить"], from: ["Dan", "С"], to: ["Gacha", "По"],
  preset_all: ["Butun davr", "Весь период"], preset_ytd: ["2026 (30.10 gacha)", "2026 (до 30.10)"], preset_12m: ["Oxirgi 12 oy", "Последние 12 мес."],
  commodity: ["Tovar", "Товар"], variant: ["Variant", "Вариант"], all: ["Barchasi", "Все"],
  region: ["Ombor hududi (sotuvchi ombori)", "Регион склада (склад продавца)"], form: ["Shartnoma shakli", "Форма контракта"],
  policy: ["Holat qoidasi", "Правило статусов"], currency: ["Shartnoma valyutasi", "Валюта контракта"],
  active_filters: ["Joriy filtrlar", "Текущие фильтры"], none: ["yo'q", "нет"],
  includes_cancelled: ["bekor qilinganlar kiritilgan", "аннулированные включены"],
  excludes_cancelled: ["bekor qilinganlar chiqarilgan", "аннулированные исключены"],
  // KPI
  kpi_deals: ["Bitimlar soni", "Количество сделок"], kpi_value: ["Bitimlar qiymati (so'mda)", "Стоимость сделок (в сумах)"],
  kpi_sellers: ["Sotuvchilar", "Продавцы"], kpi_buyers: ["Xaridorlar", "Покупатели"],
  value_note: ["Shartnomalar qiymati, tushum yoki foyda emas. USD shartnomalar bitim sanasidagi kurs bo'yicha o'girilgan",
               "Стоимость контрактов, не выручка и не прибыль. USD-контракты пересчитаны по курсу на дату сделки"],
  usd_converted: ["USD bitimlar o'girildi", "USD-сделок пересчитано"],
  cancelled_share: ["Bekor qilinganlar", "Аннулированные"], of: ["dan", "из"],
  cancelled_excluded: ["tanlangan holat qoidasi bekor qilinganlarni chiqaradi", "выбранное правило исключает аннулированные"],
  distinct_note: ["tanlangan bitimlardagi turli STIR", "различные ИНН в выбранных сделках"],
  // charts
  trend: ["Oylik savdo dinamikasi", "Помесячная динамика торгов"], by_count: ["Soni", "Количество"], by_value: ["Qiymati", "Стоимость"],
  partial_month: ["to'liq bo'lmagan oy", "неполный месяц"],
  top_commodities: ["Qiymat bo'yicha top-10 tovar", "Топ-10 товаров по стоимости"],
  top_categories: ["Qiymat bo'yicha toifalar", "Категории по стоимости"],
  of_total: ["jami", "всего"], groups: ["guruh", "групп"],
  status_dist: ["Bitim holatlari", "Статусы сделок"], denominator: ["Maxraj", "Знаменатель"],
  region_rank: ["Ombor hududlari bo'yicha qiymat", "Стоимость по регионам склада"],
  region_note: ["Sotuvchi omborining hududi (kompaniyalar hududi emas). Toshkent shahri va Toshkent viloyati alohida",
                "Регион склада продавца (не регион компаний). Ташкент-город и Ташкентская область — отдельно"],
  price_trend: ["O'rtacha tortilgan narx dinamikasi", "Динамика средневзвешенной цены"],
  price_need_variant: ["Narx grafigi uchun tovar va aniq variantni (bitta o'lchov birligi) tanlang — turli tovar va birliklar aralashtirilmaydi.",
                       "Для графика цены выберите товар и конкретный вариант (одна единица измерения) — разные товары и единицы не смешиваются."],
  price_method: ["Σ summa ÷ Σ miqdor har oy va valyuta bo'yicha; bekor qilinganlar chiqarilgan", "Σ сумм ÷ Σ количества по месяцам и валютам; аннулированные исключены"],
  bn_axis: ["mlrd so'm", "млрд сум"], deals_axis: ["bitim", "сделок"],
  table_view: ["Jadval ko'rinishi", "Таблица значений"], click_filters: ["Ustunni bosish filtr qo'shadi", "Клик по столбцу добавляет фильтр"],
  ask_view: ["Shu ko'rinish haqida so'rash", "Спросить об этом виде"],
  // states
  loading: ["Yuklanmoqda…", "Загрузка…"], empty: ["Bu filtrlar bo'yicha bitimlar yo'q (tekshirilgan).", "По этим фильтрам сделок нет (проверено)."],
  error: ["Ma'lumotni yuklab bo'lmadi", "Не удалось загрузить данные"], retry: ["Qayta urinish", "Повторить"],
  // explorer
  search: ["Bitim/shartnoma raqami yoki STIR", "Номер сделки/контракта или ИНН"], search_btn: ["Qidirish", "Найти"],
  export: ["CSV eksport", "Экспорт CSV"], export_limit: ["Eksport limiti", "Лимит экспорта"], rows: ["qator", "строк"],
  found: ["Topildi", "Найдено"], page: ["Sahifa", "Страница"], prev: ["Oldingi", "Назад"], next: ["Keyingi", "Вперёд"],
  col_id: ["№ / sana", "№ / дата"], col_product: ["Tovar", "Товар"], col_seller: ["Sotuvchi", "Продавец"], col_buyer: ["Xaridor", "Покупатель"],
  col_lots: ["Lotlar × hajm", "Лоты × размер"], col_qty: ["Miqdor", "Количество"], col_amount: ["Summa (asl)", "Сумма (исходная)"],
  col_uzs: ["Summa, so'm", "Сумма, сум"], col_status: ["Holat", "Статус"], details: ["Bitim tafsilotlari", "Детали сделки"], close: ["Yopish", "Закрыть"],
  // assistant
  ask_placeholder: ["Savolingizni o'zbek yoki rus tilida yozing…", "Напишите вопрос на узбекском или русском…"],
  send: ["Yuborish", "Отправить"], thinking: ["Savol ko'rib chiqilmoqda…", "Обрабатываю вопрос…"],
  examples: ["Misollar", "Примеры"], evidence: ["Manba / Источник", "Manba / Источник"], sql: ["Texnik tafsilotlar (SQL)", "Технические детали (SQL)"],
  context: ["Savolga qo'shiladigan kontekst", "Контекст, который будет добавлен к вопросу"], remove: ["Olib tashlash", "Убрать"],
  unavailable: ["Yangi savollarni tahlil qilish vaqtincha imkonsiz (til modeli limiti). Dashboard, bitimlar, yordam va izohlar ishlaydi.",
                "Разбор новых вопросов временно недоступен (лимит языковой модели). Дашборд, сделки, справка и пояснения работают."],
  not_configured: ["Til modeli sozlanmagan: faqat yordam va izohlar ishlaydi.", "Языковая модель не настроена: работают только справка и пояснения."],
  cancel: ["Bekor qilish", "Отмена"], sample_note: ["Bir nechta misol bitimlar; to'liq ro'yxat emas", "Несколько примеров сделок; не полный список"],
  // redesign: shell, headers
  menu_open: ["Menyuni ochish", "Открыть меню"], menu_close: ["Menyuni yopish", "Закрыть меню"], main_nav: ["Asosiy bo'limlar", "Основные разделы"],
  lang_group: ["Interfeys tili", "Язык интерфейса"], prototype_short: ["Hakaton prototipi", "Прототип для хакатона"],
  not_official: ["Rasmiy UZEX xizmati emas", "Не официальный сервис UZEX"],
  footer_note: ["Barcha raqamlar taqdim etilgan sintetik ma'lumotlar bazasidan hisoblanadi.", "Все цифры рассчитаны по предоставленной синтетической базе."],
  status_on: ["Holat", "Статусы на"], dash_title: ["Savdo tahlili", "Анализ торгов"], ask_ai: ["AI'dan so'rash", "Спросить AI"],
  deals_title: ["Bitimlar", "Сделки"], ai_title: ["AI yordamchi", "AI-ассистент"],
  ai_lead: ["Birja ma'lumotlariga asoslangan javoblar", "Ответы на основе данных биржи"],
  // filters (compact)
  region_short: ["Ombor hududi", "Регион склада"], period_custom: ["Boshqa sana…", "Другие даты…"], more_filters: ["Qo'shimcha filtrlar", "Дополнительные фильтры"],
  active_count: ["faol", "активно"], done: ["Tayyor", "Готово"], policy_all_incl: ["Barcha bitimlar · Bekor qilinganlar kiritilgan", "Все сделки · Аннулированные включены"],
  // KPI (compact)
  kpi_contract_value: ["Shartnomalar qiymati", "Стоимость контрактов"], not_revenue: ["Tushum yoki foyda emas", "Не выручка и не прибыль"],
  exact_value: ["Aniq qiymat", "Точное значение"], how_calc: ["Hisoblash usuli", "Метод расчёта"],
  cancel_method: ["Ulush = bekor qilingan (to'lanmagan) bitimlar ÷ joriy filtrlar bo'yicha barcha bitimlar × 100. Kasr qismi faqat ko'rsatishda yaxlitlanadi.",
                  "Доля = аннулированные (неоплаченные) сделки ÷ все сделки по текущим фильтрам × 100. Округление только при выводе."],
  usd_note: ["USD shartnomalar bitim sanasidagi kurs bo'yicha so'mga o'girilgan", "USD-контракты пересчитаны в сумы по курсу на дату сделки"],
  ask_card_title: ["Ma'lumotlardan so'rang", "Спросите у данных"], ask_card_btn: ["Savol berish", "Задать вопрос"],
  ask_card_ctx: ["Joriy filtrlar savolga kontekst sifatida qo'shiladi (AI sahifasida olib tashlash mumkin).",
                 "Текущие фильтры будут добавлены к вопросу как контекст (можно убрать на странице AI)."],
  ask_card_label: ["Ma'lumotlar haqida savol", "Вопрос о данных"],
  // deals (compact)
  deals_count: ["ta bitim", "сделок"], export_need_filter: ["Eksport uchun natijani 50 000 qatorgacha filtrlang", "Для экспорта сузьте результат до 50 000 строк"],
  export_ready: ["Barcha filtrlangan qatorlar CSV ga yuklanadi", "В CSV попадут все отфильтрованные строки"],
  col_deal: ["Bitim / sana", "Сделка / дата"], col_parties: ["Sotuvchi / xaridor", "Продавец / покупатель"], col_amount_uzs: ["Summa (UZS)", "Сумма (UZS)"],
  seller_tag: ["Sotuvchi", "Продавец"], buyer_tag: ["Xaridor", "Покупатель"], open_details: ["tafsilotlarini ochish", "открыть детали"],
  per_page: ["sahifada", "на странице"], sort_by: ["Saralash", "Сортировка"], desc: ["kamayish", "по убыванию"], asc: ["o'sish", "по возрастанию"],
  table_label: ["Bitimlar jadvali", "Таблица сделок"], clear_search: ["Qidiruvni tozalash", "Очистить поиск"], pages_nav: ["Sahifalar", "Страницы"],
  // assistant (compact)
  ai_welcome: ["Ma'lumotlardan so'rang", "Спросите у данных"],
  ai_explain: ["Savolingizni o'zbek yoki rus tilida yozing. Javoblar bazadan hisoblanadi va manbasi bilan ko'rsatiladi.",
               "Напишите вопрос на узбекском или русском. Ответы рассчитываются по базе и показываются с источником."],
  composer_label: ["Savolingiz", "Ваш вопрос"], enter_hint: ["Enter — yuborish · Shift + Enter — yangi qator", "Enter — отправить · Shift + Enter — новая строка"],
  time_limit: ["Javob uchun vaqt chegarasi", "Лимит времени на ответ"], seconds: ["soniya", "секунд"], source_each: ["Har bir javobda manba", "Источник в каждом ответе"],
  you: ["Siz", "Вы"], assistant_name: ["Yordamchi", "Ассистент"], new_chat: ["Yangi suhbat", "Новый диалог"],
  tag_quota: ["Xizmat limiti — ma'lumot yo'qligi emas", "Лимит сервиса — это не отсутствие данных"], tag_nodata: ["Ma'lumot yo'q", "Нет данных"],
  tag_clar: ["Aniqlashtirish kerak", "Нужно уточнение"], tag_fail: ["Xato", "Ошибка"],
} as const;

export const SUGGESTIONS: Record<Lang, { kind: string; q: string; icon: string }[]> = {
  uz: [
    { kind: "Bitimlar soni", q: "2025-yilda nechta bitim tuzilgan?", icon: "handshake" },
    { kind: "Sotuvchilar reytingi", q: "2025-yilda summa bo‘yicha eng yirik 5 ta sotuvchi kim?", icon: "building" },
    { kind: "Bitim tafsilotlari", q: "387831-raqamli bitim haqida ma’lumot", icon: "file" },
    { kind: "Imkoniyatlar", q: "Nimalarni so‘ray olaman?", icon: "help" },
  ],
  ru: [
    { kind: "Количество сделок", q: "Сколько сделок было заключено в 2025 году?", icon: "handshake" },
    { kind: "Рейтинг продавцов", q: "Кто 5 крупнейших продавцов по сумме сделок в 2025 году?", icon: "building" },
    { kind: "Детали сделки", q: "Покажи информацию о сделке 387831", icon: "file" },
    { kind: "Возможности", q: "Что ты умеешь?", icon: "help" },
  ],
};

export type Key = keyof typeof D;
export const t = (k: Key, lang: Lang): string => D[k][lang === "uz" ? 0 : 1];

export const EXAMPLES: Record<Lang, string[]> = {
  uz: ["2025-yilda nechta bitim tuzilgan?", "2025-yilda bitimlar summasi bo'yicha eng yirik 5 ta sotuvchi kim?",
       "387831-raqamli bitim haqida ma'lumot", "nimalarni so'ray olaman", "bu natijani tushuntir"],
  ru: ["Сколько сделок было заключено в 2025 году?", "Топ-5 продавцов по сумме сделок в 2025 году",
       "Покажи сделку 387831", "Что ты умеешь?", "Объясни этот результат"],
};
