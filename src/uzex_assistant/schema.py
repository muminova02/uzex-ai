"""Source schema (exact CSV headers, verified in docs/data_audit.md A2) and dimension data."""
from __future__ import annotations

# Bump whenever table/view definitions or conversion rules change: forces a rebuild.
SCHEMA_VERSION = "2.1.0"

DEAL_COLUMNS = [
    "Номер сделки", "Дата сделки", "Время сделки", "Тип торгов", "Количество", "Сумма сделки", "Стартовая цена",
    "Номер контракта", "Наименование товара", "Лот", "Единица измерения", "Регион", "Тип контракта",
    "Форма контракта", "Состояние сделки", "ИНН продавца", "ИНН покупателя", "Брокер продавца",
    "Брокер покупателя", "Дата оплаты", "Дата поставки", "Поставлено",
]
CONTRACT_COLUMNS = [
    "Номер контракта", "Наименование продукта", "Марка товара", "Размер товара", "Сорт товара", "Тип товара",
    "Класс товара", "Код ТН ВЭД", "Группа товара", "Категория товара", "Отрасль промышленности",
    "Единица измерения", "Лот", "Валюта", "Базисная цена", "Сумма залога", "Базис поставки", "Регион",
    "Адрес склада", "Тип торгов", "Тип контракта", "Форма контракта", "Срок оплаты", "Срок поставки",
    "Упаковка", "ИНН продавца", "Дата регистрации", "Действует до",
]
PARTICIPANT_COLUMNS = ["ИНН", "Наименование", "Регион", "Вид деятельности", "Роль", "Плательщик НДС"]
BROKER_COLUMNS = ["Код брокера", "Наименование", "ИНН", "Регион"]
RATE_COLUMNS = ["Дата", "Курс"]

DEAL_FILE_GLOB = "deals_*.csv"
REFERENCE_FILES = {
    "contracts": ("contracts.csv", CONTRACT_COLUMNS),
    "participants": ("participants.csv", PARTICIPANT_COLUMNS),
    "brokers": ("brokers.csv", BROKER_COLUMNS),
    "usd_rates": ("usd_rates.csv", RATE_COLUMNS),
}

# raw status -> (code, is_paid, is_cancelled, is_fully_delivered)
STATUSES: dict[str, tuple[str, bool, bool, bool]] = {
    "Новый": ("new", False, False, False),
    "Не подписан": ("unsigned", False, False, False),
    "Заключен": ("concluded", False, False, False),
    "Ждёт оплату": ("awaiting_payment", False, False, False),
    "Оплачен": ("paid", True, False, False),
    "Исполнен(Частично)": ("partially_fulfilled", True, False, False),
    "Исполнен": ("fulfilled", True, False, True),
    "Аннулирован(Не оплачен)": ("cancelled_unpaid", False, True, False),
}

# Named status policies (docs/solution_spec.md §4). No universal filter is applied anywhere.
STATUS_POLICIES: dict[str, tuple[str, ...]] = {
    "registry": tuple(code for code, *_ in STATUSES.values()),
    "not_cancelled": ("new", "unsigned", "concluded", "awaiting_payment", "paid", "partially_fulfilled", "fulfilled"),
    "paid": ("paid", "partially_fulfilled", "fulfilled"),
    "fulfilled": ("fulfilled",),
    "fulfilled_any": ("fulfilled", "partially_fulfilled"),
    "active": ("new", "unsigned", "concluded", "awaiting_payment", "paid", "partially_fulfilled"),
    "awaiting_payment_any": ("new", "unsigned", "concluded", "awaiting_payment"),
    "cancelled": ("cancelled_unpaid",),
}

TRADE_TYPES = {"Английский аукцион": "english_auction", "Обратный аукцион": "reverse_auction",
               "Двусторонний аукцион": "double_auction"}
CONTRACT_TYPES = {"Внутренний": "domestic", "Экспортный": "export"}
CONTRACT_FORMS = {"Спот": "spot", "Форвард": "forward", "Фьючерс": "futures"}
CURRENCIES = {"Узбекский сум": "UZS", "Доллар США": "USD"}
ROLES = {"Продавец": "seller", "Покупатель": "buyer"}
VAT_FLAGS = {"Да": True, "Нет": False}
