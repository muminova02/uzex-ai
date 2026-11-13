import sys, glob, pandas as pd
from decimal import Decimal
d = sys.argv[1]
deals = pd.concat([pd.read_csv(f, dtype=str, keep_default_na=False) for f in sorted(glob.glob(f"{d}/deals_*.csv"))])
c = pd.read_csv(f"{d}/contracts.csv", dtype=str, keep_default_na=False)
p = pd.read_csv(f"{d}/participants.csv", dtype=str, keep_default_na=False)
b = pd.read_csv(f"{d}/brokers.csv", dtype=str, keep_default_na=False)
r = pd.read_csv(f"{d}/usd_rates.csv", dtype=str, keep_default_na=False)
print("deals", len(deals), "unique ids", deals["Номер сделки"].nunique())
print(deals["Состояние сделки"].value_counts().to_string())
for col in ["Тип торгов","Тип контракта","Форма контракта","Единица измерения","Регион"]:
    print("--",col, deals[col].nunique(), deals[col].value_counts().head(15).to_dict())
print("-- contract currency", c["Валюта"].value_counts().to_dict())
m = deals.merge(c[["Номер контракта","Валюта","Лот","Тип контракта","ИНН продавца","Единица измерения"]], on="Номер контракта", how="left", suffixes=("","_c"))
print("deals w/o contract", m["Валюта"].eq("").sum()+m["Валюта"].isna().sum())
print("lot mismatch", (m["Лот"]!=m["Лот_c"]).sum(), "unit mismatch", (m["Единица измерения"]!=m["Единица измерения_c"]).sum(),
      "seller mismatch", (m["ИНН продавца"]!=m["ИНН продавца_c"]).sum(), "type mismatch",(m["Тип контракта"]!=m["Тип контракта_c"]).sum())
print(pd.crosstab(m["Тип контракта"], m["Валюта"]))
ids=set(p["ИНН"]); print("unknown sellers", (~deals["ИНН продавца"].isin(ids)).sum(), "unknown buyers", (~deals["ИНН покупателя"].isin(ids)).sum())
bk=set(b["Код брокера"]); print("unknown brokers", (~deals["Брокер продавца"].isin(bk)).sum(), (~deals["Брокер покупателя"].isin(bk)).sum())
print("participant INN dup", p["ИНН"].duplicated().sum(), "roles", p["Роль"].value_counts().to_dict())
both = set(deals["ИНН продавца"]) & set(deals["ИНН покупателя"]); print("INNs both seller&buyer", len(both))
print("name dups", p["Наименование"].duplicated().sum())
dates=set(r["Дата"]); print("deal dates missing rate", (~deals["Дата сделки"].isin(dates)).sum(), "rates range", r["Дата"].iloc[0], r["Дата"].iloc[-1], len(r))
# number formats
bad=[]
for col in ["Сумма сделки","Стартовая цена","Количество","Лот","Поставлено"]:
    s=deals[col].str.replace(" ","").str.replace(" ","")
    ok=s.str.fullmatch(r"-?\d+(\.\d+)?|")
    print(col,"nonnumeric",(~ok).sum(),"empty",(s=="").sum(), "samples", deals.loc[~ok,col].head(3).tolist())
print("Lot values", deals["Лот"].value_counts().head(10).to_dict())
print("decimals in sum:", deals["Сумма сделки"].str.extract(r"\.(\d+)$")[0].str.len().value_counts().to_dict())
st=deals["Состояние сделки"]
for s in st.unique():
    sub=deals[st==s]; print(f"{s:28s} pay_empty={ (sub['Дата оплаты']=='').mean():.2f} del_empty={(sub['Дата поставки']=='').mean():.2f} delivered_empty={(sub['Поставлено']=='').mean():.2f}")
print("deal names != normalized contract names sample:")
print(c[["Наименование продукта","Марка товара","Сорт товара","Группа товара","Категория товара"]].sample(8, random_state=1).to_string())
print("contract regions", c["Регион"].unique()[:20])
print("participant regions", p["Регион"].unique()[:20])
