"""对本地 Compose 的 Python→MySQL 链路做独立 CSV 核对；不读取/打印宿主密钥。"""
import argparse
import csv
import json
import subprocess
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path


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


def run(args):
    data=facts(args.csv_dir)
    cases=[
        ("本月 GMV 和订单量和客单价按州统计",["gmv","orders","aov"],["state"],"2018-08-01",lambda r:True,None),
        ("本月取消率和平均交付时长和平均评分按州统计",["cancellation_rate","delivery_days","review_score"],["state"],"2018-08-01",lambda r:True,None),
        ("最近 6 个月 GMV 和客单价按月统计",["gmv","aov"],["month"],"2018-03-01",lambda r:True,None),
        ("最近 3 个月 GMV 和订单量按月和州统计",["gmv","orders"],["month","state"],"2018-06-01",lambda r:True,None),
        ("本月商品金额大于 100 的有效订单量",["orders"],[],"2018-08-01",lambda r:r["amount"] is not None and r["amount"]>100,None),
        ("本月 GMV 不含 SP 州",["gmv"],[],"2018-08-01",lambda r:r["state"]!="SP",None),
        ("本月只看已取消订单的有效订单量",["orders"],[],"2018-08-01",lambda r:r["status"]=="canceled",None),
        ("本月 GMV 按州排名，取前 5 名",["gmv"],["state"],"2018-08-01",lambda r:True,5),
    ]
    if args.with_model:
        cases.append(("本月 GMV 和订单量，只看圣保罗州",["gmv","orders"],[],"2018-08-01",lambda r:r["state"]=="SP",None))
    requests=[{"tenant_id":args.tenant,"role":args.role,"allowed_metrics":["gmv","orders","aov","cancellation_rate","delivery_days","review_score"],"input":{"question":case[0]}} for case in cases]
    probe="""import json,os,sys,httpx
requests=json.loads(sys.stdin.read())
with httpx.Client(timeout=35,trust_env=False) as client:
 results=[]
 for body in requests:
  response=client.post('http://127.0.0.1:8000/internal/query',headers={'X-Service-Key':os.environ['SERVICE_KEY']},json=body)
  results.append({'http_status':response.status_code,'result':response.json()})
 print(json.dumps(results,ensure_ascii=False))
"""
    response=subprocess.run(["docker","compose","exec","-T","agent","python","-c",probe],input=json.dumps(requests,ensure_ascii=False).encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=90)
    assert response.returncode==0, "分析容器验证进程失败"
    results=json.loads(response.stdout.decode("utf-8"))
    for index,(case,actual) in enumerate(zip(cases,results,strict=True)):
        question,metrics,dimensions,start,predicate,top=case
        result=actual["result"]
        assert actual["http_status"]==200 and result["status"]=="completed", (question,actual["http_status"],result.get("message") or result.get("detail"))
        groups=defaultdict(list)
        for row in data:
            if start<=row["date"]<"2018-09-01" and predicate(row):
                key=tuple(row["date"][:7] if d=="month" else row[d] for d in dimensions)
                groups[key].append(row)
        if not dimensions and not groups: groups[()]=[]
        expected={key:calculate(rows) for key,rows in groups.items()}
        keys=sorted(expected)
        if top: keys=sorted(keys,key=lambda key:(-expected[key][0]["gmv"],key))[:top]
        assert [tuple(row["dimensions"][d] for d in dimensions) for row in result["rows"]]==keys,question
        for row,key in zip(result["rows"],keys,strict=True):
            values,samples=expected[key]
            assert row["total_orders"]==len(groups[key])
            assert row["unavailable_orders"]==sum(r["status"]=="unavailable" for r in groups[key])
            for metric in metrics:
                cell=row["values"][metric];value=cell["value"]
                assert (value is None)==(values[metric] is None),(question,key,metric)
                if value is not None: assert abs(Decimal(str(value))-values[metric])<=(Decimal(0) if metric in ("gmv","orders") else Decimal("0.0001")),(question,key,metric)
                assert cell["sample_size"]==samples[metric] and cell["excluded_orders"]==len(groups[key])-samples[metric]
        expected_parser="llm" if args.with_model and index==len(cases)-1 else "rules"
        assert result["usage"]["parser"]==expected_parser
        if expected_parser=="rules": assert result["usage"]["tokens"]==0
        print(json.dumps({"question":question,"groups":len(keys),"parser":expected_parser,"tokens":result["usage"]["tokens"],"csv_match":True},ensure_ascii=False))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv-dir",type=Path,default=Path("data/olist"))
    parser.add_argument("--tenant",default="alpha")
    parser.add_argument("--role",choices=["admin","ops","data"],default="admin")
    parser.add_argument("--with-model",action="store_true",help="增加一次付费模型兜底验证")
    run(parser.parse_args())
