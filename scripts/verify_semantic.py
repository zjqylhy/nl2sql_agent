"""独立 Olist CSV oracle，供真实 PostgreSQL 的导入与指标口径核对。"""
import csv
from collections import defaultdict
from datetime import datetime
from decimal import Decimal


def read(folder, name):
    with (folder / f"olist_{name}_dataset.csv").open(encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def facts(folder):
    states = {r["customer_id"]:r["customer_state"] for r in read(folder,"customers")}
    amounts=defaultdict(Decimal)
    for row in read(folder,"order_items"): amounts[row["order_id"]]+=Decimal(row["price"])
    reviews={}
    for row in read(folder,"order_reviews"):
        score=int(row["review_score"])
        key=(row["review_creation_date"],row["review_answer_timestamp"],row["review_id"],score)
        if 1<=score<=5 and (row["order_id"] not in reviews or key>reviews[row["order_id"]][0]): reviews[row["order_id"]]=(key,score)
    result=[]
    for row in read(folder,"orders"):
        delivery=None
        if row["order_status"]=="delivered" and row["order_delivered_customer_date"]:
            seconds=int((datetime.fromisoformat(row["order_delivered_customer_date"])-datetime.fromisoformat(row["order_purchase_timestamp"])).total_seconds())
            if seconds>=0: delivery=Decimal(seconds)/Decimal(86400)
        result.append({"date":row["order_purchase_timestamp"],"state":states[row["customer_id"]],"status":row["order_status"],"amount":amounts.get(row["order_id"]),"delivery":delivery,"review":Decimal(reviews[row["order_id"]][1]) if row["order_id"] in reviews else None})
    return result


def calculate(rows):
    valid=[r for r in rows if r["status"] not in ("canceled","unavailable")]
    priced=[r for r in valid if r["amount"] is not None]
    gmv=sum((r["amount"] for r in priced),Decimal(0)) if valid and len(priced)==len(valid) else None
    delivery=[r["delivery"] for r in rows if r["delivery"] is not None]
    review=[r["review"] for r in rows if r["review"] is not None]
    values={"gmv":gmv,"orders":Decimal(len(valid)) if rows else None,"aov":gmv/len(valid) if gmv is not None else None,"cancellation_rate":Decimal(100*sum(r["status"]=="canceled" for r in rows))/len(rows) if rows else None,"delivery_days":sum(delivery)/len(delivery) if delivery else None,"review_score":sum(review)/len(review) if review else None}
    samples={"gmv":len(priced),"orders":len(valid),"aov":len(valid),"cancellation_rate":len(rows),"delivery_days":len(delivery),"review_score":len(review)}
    return values,samples
