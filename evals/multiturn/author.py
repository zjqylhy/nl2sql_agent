"""人工草稿的显式条件。冻结 JSON 后再运行，禁止用解析结果回填期望。"""
from copy import deepcopy
import json
from pathlib import Path

MONTH={"start":"2018-08-01","end_exclusive":"2018-09-01"}
LAST={"start":"2018-07-01","end_exclusive":"2018-08-01"}
HALF={"start":"2018-03-01","end_exclusive":"2018-09-01"}

def p(**kw):
    return {"version":1,"metrics":["gmv"],"dimensions":["state"],"time_range":MONTH,"filters":[],"order_by":[],"top_n":None,**kw}

def f(field,values,op="in"): return {"field":field,"op":op,"values":values}
def order(metric="gmv",direction="desc"): return [{"field":metric,"direction":direction}]
def ok(q,plan): return {"question":q,"status":"completed","expected":plan,"pending_missing":[]}
def clarify(q,base,missing=(),values=None):
    return {"question":q,"status":"needs_clarification","expected":base,"pending_missing":list(missing),"pending_values":values or {}}
chains=[]
def chain(name,turns,category="rules",identity="admin"):
    n=len(chains)+1; previous=None
    for t in turns:
        t["base"]=deepcopy(previous)
        t["changed_fields"]=[k for k in p() if k!="version" and (previous or {}).get(k)!= (t["expected"] or {}).get(k)] if t["status"]=="completed" else []
        t["preserved_fields"]=[k for k in p() if k!="version" and k not in t["changed_fields"]]
        previous=deepcopy(t["expected"])
    chains.append({"id":f"chain-{n:02}","intent":name,"group":name,"split":"acceptance" if n%2==0 else "development","category":category,"identity":identity,"turns":turns})

chain("半年分组及两次补答",[ok("最近半年 GMV 按月统计",p(dimensions=["month"],time_range=HALF)),ok("改成按州",p(time_range=HALF)),clarify("只看两个州，取前三名",p(time_range=HALF),["states","order_by"],{"top_n":3}),clarify("SP、RJ",p(time_range=HALF),["order_by"],{"top_n":3}),ok("按 GMV 从高到低",p(time_range=HALF,filters=[f("state",["SP","RJ"])],order_by=order(),top_n=3))])
chain("指标增加及移除",[ok("本月 GMV 按州统计",p()),ok("再加上订单量",p(metrics=["gmv","orders"])),ok("再加上平均评分",p(metrics=["gmv","orders","review_score"])),ok("去掉 GMV",p(metrics=["orders","review_score"]))])
chain("固定基准日期",[ok("本月 GMV 按州统计",p()),ok("那上个月呢",p(time_range=LAST)),ok("本月",p()),ok("2018-06-01 至 2018-07-01",p(time_range={"start":"2018-06-01","end_exclusive":"2018-07-01"}))])
chain("州集合增加",[ok("本月 GMV 按州统计",p()),ok("只看 SP",p(filters=[f("state",["SP"])])),ok("再加 RJ 州",p(filters=[f("state",["SP","RJ"])]))])
chain("清除州保留金额",[ok("本月 GMV 按州统计",p()),ok("商品金额大于 100",p(filters=[f("amount",["100"],"gt")])),ok("只看 SP",p(filters=[f("amount",["100"],"gt"),f("state",["SP"])])),ok("取消州筛选",p(filters=[f("amount",["100"],"gt")]))])
chain("状态条件替换清除",[ok("本月订单量按州统计",p(metrics=["orders"])),ok("订单状态为 delivered",p(metrics=["orders"],filters=[f("status",["delivered"])])),ok("订单状态为 shipped",p(metrics=["orders"],filters=[f("status",["shipped"])])),ok("取消状态筛选",p(metrics=["orders"]))])
chain("金额运算符替换",[ok("本月客单价按州统计",p(metrics=["aov"])),ok("商品金额至少 100",p(metrics=["aov"],filters=[f("amount",["100"],"gte")])),ok("商品金额小于 200",p(metrics=["aov"],filters=[f("amount",["200"],"lt")])),ok("取消金额筛选",p(metrics=["aov"]))])
chain("排除州继承",[ok("本月 GMV 按州统计",p()),ok("排除 SP",p(filters=[f("state",["SP"],"not_in")])),ok("改成订单量",p(metrics=["orders"],filters=[f("state",["SP"],"not_in")])),ok("取消州筛选",p(metrics=["orders"]))])
chain("排名数量与方向",[ok("本月 GMV 按州统计",p()),ok("按 GMV 从高到低，取前 3 名",p(order_by=order(),top_n=3)),ok("取前 2 名",p(order_by=order(),top_n=2)),ok("按 GMV 从低到高",p(order_by=order(direction="asc"),top_n=2))])
chain("整体清理排名",[ok("本月 GMV 按州排名，从高到低取前 3 名",p(order_by=order(),top_n=3)),ok("改成整体",p(dimensions=[])),ok("改成按月",p(dimensions=["month"]))])
chain("分组清理失效排序",[ok("最近 6 个月 GMV 按月统计，按月份降序",p(time_range=HALF,dimensions=["month"],order_by=order("month"))),ok("改成按州",p(time_range=HALF)),ok("改成按日",p(time_range=HALF,dimensions=["day"]))])
chain("换指标保留排名草稿",[ok("本月 GMV 按州排名，从高到低取前 3 名",p(order_by=order(),top_n=3)),clarify("改成订单量",p(order_by=order(),top_n=3),["order_by"]),ok("按订单量从低到高",p(metrics=["orders"],order_by=order("orders","asc"),top_n=3))])
chain("初始缺时间补答",[clarify("GMV 按州统计",None,["time_range"]),ok("本月",p()),ok("改成按月",p(dimensions=["month"]))])
chain("取消草稿恢复有效计划",[ok("本月 GMV 按州统计",p()),clarify("只看两个州，取前三名",p(),["states","order_by"],{"top_n":3}),{"action":"discard","status":"discarded","expected":p(),"pending_missing":[]},ok("改成按月",p(dimensions=["month"]))])
chain("明确重新开始",[ok("本月 GMV 按州统计",p()),clarify("只看两个州，取前三名",p(),["states","order_by"],{"top_n":3}),ok("重新开始，上月订单量按月统计",p(metrics=["orders"],dimensions=["month"],time_range=LAST))])

# 陌生表达在不同指标、时间与分组下逐链标注，不能仅复用 SQL 成功标签。
for i,(metric,title,dimension,window,question,field,values,op) in enumerate([
 ("gmv","GMV","state",MONTH,"只看圣保罗州","state",["SP"],"in"),
 ("orders","订单量","state",MONTH,"只看里约热内卢州","state",["RJ"],"in"),
 ("aov","客单价","month",HALF,"只看圣保罗州","state",["SP"],"in"),
 ("review_score","平均评分","state",LAST,"只看里约热内卢州","state",["RJ"],"in"),
 ("delivery_days","平均交付时长","month",HALF,"排除圣保罗州","state",["SP"],"not_in"),
 ("cancellation_rate","取消率","state",LAST,"排除里约热内卢州","state",["RJ"],"not_in"),
 ("gmv","GMV","state",MONTH,"商品金额至少一百雷亚尔","amount",["100"],"gte"),
 ("orders","订单量","month",HALF,"订单状态只保留已送达的","status",["delivered"],"in"),
 ("aov","客单价","state",LAST,"商品金额低于两百雷亚尔","amount",["200"],"lt"),
 ("review_score","平均评分","month",HALF,"订单状态不要取消的","status",["canceled"],"not_in"),
]):
    plan=p(metrics=[metric],dimensions=[dimension],time_range=window)
    time="最近 6 个月" if window==HALF else "上月" if window==LAST else "本月"
    dim="月" if dimension=="month" else "州"
    filtered={**plan,"filters":[f(field,values,op)]}
    chain(f"陌生追问-{i+1:02}",[ok(f"{time}{title}按{dim}统计",plan),ok(question,filtered),ok("改成整体",{**filtered,"dimensions":[]})],"unfamiliar")

chain("范围外保留已有草稿",[ok("本月 GMV 按州统计",p()),clarify("只看两个州，取前三名",p(),["states","order_by"],{"top_n":3}),clarify("改成按品类",p(),["states","order_by"],{"top_n":3}),ok("SP、RJ，按 GMV 从高到低",p(filters=[f("state",["SP","RJ"])],order_by=order(),top_n=3))],"boundary")
chain("冲突后继续",[ok("本月 GMV 按州统计",p()),clarify("改成本月和上月",p()),ok("改成上月",p(time_range=LAST))],"boundary")
chain("初始缺指标补答",[clarify("本月按州统计",None,["metrics"]),ok("GMV",p()),ok("那上个月呢",p(time_range=LAST))],"boundary")
chain("追问越权不推进",[ok("本月 GMV 按州统计",p()),{"question":"再加上平均评分","status":"denied","expected":p(),"pending_missing":[]},ok("改成按月",p(dimensions=["month"]))],"boundary","ops")
chain("无前文澄清",[clarify("改成按州",None,["metrics","time_range"]),clarify("GMV",None,["time_range"]),ok("本月",p())],"boundary")

if __name__=="__main__":
    target=Path(__file__).with_name("chains.json")
    target.write_text(json.dumps({"version":1,"annotation_status":"draft","reviewer":None,"analysis_date":"2018-08-31","chains":chains},ensure_ascii=False,indent=2)+"\n",encoding="utf-8",newline="\n")
