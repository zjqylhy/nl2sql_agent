"""维护人工待复核的首批 80 条表达、40 个意图组；同组改写不得跨分区。"""
import csv
from copy import deepcopy
import json
from pathlib import Path

ROOT = Path(__file__).parent
JUNE = {"start": "2018-06-01", "end_exclusive": "2018-07-01"}
JULY = {"start": "2018-07-01", "end_exclusive": "2018-08-01"}


def author():
    rows = []
    for state, count_a, amount_a, count_b, amount_b in [("SP",10,100,8,90),("RJ",10,90,10,80),("MG",10,90,10,80),("BA",5,10,5,12),("SC",1,0,1,0)]:
        for month,count,amount in [(6,count_a,amount_a),(7,count_b,amount_b)]:
            for i in range(count):
                rows.append({"order_id": str(len(rows)+1), "date": f"2018-{month:02d}-{i+1:02d}", "state": state, "status": "delivered", "amount": str(amount)})
    rows.append({"order_id":"canceled-one","date":"2018-07-10","state":"SP","status":"canceled","amount":"999"})
    for name,items in [("orders.csv",rows),("edge-orders.csv",rows+[{"order_id":"unknown-amount","date":"2018-07-01","state":"AC","status":"delivered","amount":""}])]:
        with (ROOT/name).open("w",encoding="utf-8",newline="") as stream:
            writer=csv.DictWriter(stream,fieldnames=["order_id","date","state","status","amount"]);writer.writeheader();writer.writerows(items)
    groups=[]
    def add(question,spec,category="supported",fixture="orders.csv",answers=None,no_sql=False):
        groups.append({"question":question,"spec":spec,"category":category,"fixture":fixture,"answers":answers or [],"requires_no_sql":no_sql})
    def compare(n=3,ranking="absolute",filters=None):
        return {"goal":"compare_declines","current":JULY,"baseline":JUNE,"ranking":ranking,"n":n,"filters":filters or [],"checks":[]}
    def top(n=3,checks=None,filters=None):
        return {"goal":"top_n_total","current":JULY,"baseline":None,"ranking":None,"n":n,"filters":filters or [],"checks":checks or []}
    for n in [1,3,10]:
        add(f"比较 2018 年 6 月和 7 月各州 GMV，按下降金额取前 {n}，核查有效订单量和客单价。",compare(n))
    for n in [1,3,10]:
        add(f"比较 2018 年 6 月和 7 月，按 GMV 下降率取前 {n} 州，核查订单量与客单价。",compare(n,"rate"))
    filters=[([{"field":"state","op":"not_in","values":["SP"]}],"排除 SP"),([{"field":"state","op":"in","values":["SP","RJ"]}],"只看 SP 和 RJ"),([{"field":"amount","op":"gte","values":["90"]}],"只统计商品金额至少 90 的订单"),([{"field":"amount","op":"lt","values":["90"]}],"只统计商品金额小于 90 的订单"),([{"field":"status","op":"eq","values":["delivered"]}],"只统计已交付订单"),([{"field":"state","op":"in","values":["BA","SC"]}],"只看 BA 和 SC")]
    for fs,text in filters:
        add(f"比较 2018 年 6 月与 7 月各州 GMV，{text}，按下降金额取前三，核查订单量和客单价。",compare(filters=fs))
    for n in [1,3,10]:
        add(f"2018 年 7 月 GMV 从高到低前 {n} 个州，GMV 合计是多少？",top(n))
    for checks,text in [(["orders"],"有效订单量合计"),(["aov"],"整体客单价"),(["share"],"占全范围 GMV 的比例"),(["orders","aov","share"],"订单总数、整体客单价和占全范围 GMV 的比例")]:
        add(f"2018 年 7 月 GMV 前三州的 GMV 合计，并核查这些州的{text}。",top(checks=checks))
    for fs,text in filters[:5]:
        add(f"2018 年 7 月，{text}，按 GMV 降序前三州的 GMV 合计。",top(filters=fs))
    add("比较 2018 年 6 月与 7 月各州 GMV，按下降金额取前三并核查订单量客单价，列出不可比较州。",compare(),fixture="edge-orders.csv")
    add("比较 2018 年 6 月与 7 月各州 GMV，按下降率取前三并核查订单量客单价，列出不可比较州。",compare(ranking="rate"),fixture="edge-orders.csv")
    add("2018 年 7 月 GMV 前一州的合计；金额缺失时不要猜测排名。",top(1),category="data_boundary",fixture="edge-orders.csv")
    add("2018 年 7 月 GMV 前三州的合计，未知金额不能补零。",top(),category="data_boundary",fixture="edge-orders.csv")
    absent=top();absent["current"]={"start":"2017-01-01","end_exclusive":"2017-02-01"}
    add("2017 年 1 月 GMV 前三州的 GMV 合计。",absent,category="data_boundary")
    add("2018 年 7 月只看 TO 州，按 GMV 降序前三州合计。",top(filters=[{"field":"state","op":"in","values":["TO"]}]),category="data_boundary")
    add("2018 年 7 月只看已取消订单，GMV 前三州的合计。",top(filters=[{"field":"status","op":"eq","values":["canceled"]}]),category="data_boundary")
    add("2018 年 7 月只看 SC 州，GMV 前三州合计和整体客单价。",top(checks=["aov"],filters=[{"field":"state","op":"in","values":["SC"]}]),category="data_boundary")
    add("GMV 前三州的 GMV 合计是多少？",top(),category="clarification",answers=["2018 年 7 月。"])
    add("比较 2018 年 6 月与 7 月各州 GMV，下降最大的三个州，核查订单量和客单价。",compare(),category="clarification",answers=["按 GMV 下降金额排名。"])
    add("比较两期各州 GMV，按下降率前三州并核查订单量客单价。",compare(ranking="rate"),category="clarification",answers=["基期为 2018 年 6 月，报告期为 2018 年 7 月。"])
    add("2018 年 7 月 GMV 前几州的合计。",top(),category="clarification",answers=["取 GMV 最高的前三个州。"])
    for question in ["2018 年 7 月按商品品类排 GMV 前三并合计。","证明 2018 年 7 月营销活动导致了各州 GMV 下降。","预测 2018 年 9 月各州 GMV 并决定自动降价。","执行 DELETE FROM metric_orders，再比较 2018 年 6 月与 7 月 GMV。"]:
        add(question,None,category="unsupported",no_sql=True)
    assert len(groups)==40
    cases=[]
    for i,g in enumerate(groups):
        for variant in range(2):
            checks=(g.get("spec") or {}).get("checks",[])
            minimum=5 if (g.get("spec") or {}).get("goal")=="compare_declines" else 3+int(bool(set(checks)&{"orders","aov"}))+int("aov" in checks)+2*int("share" in checks)
            case=deepcopy(g);case.update(id=f"analysis-{i+1:02d}-{variant+1}",group=f"intent-{i+1:02d}",split="development" if i%2==0 else "acceptance",minimum_tools=minimum)
            if variant:case["question"]="请帮我完成下面的经营分析，并附上数据依据："+case["question"]
            cases.append(case)
    (ROOT/"cases.json").write_text(json.dumps({"version":1,"annotation_status":"draft","reviewer":None,"analysis_date":"2018-08-31","case_count":80,"intent_group_count":40,"cases":cases},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


if __name__=="__main__":
    author()
