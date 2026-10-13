"""以整条意图链隔离分区，黄金计划与运行时实现独立。"""
import json
from pathlib import Path
from typing import Literal
from pydantic import Field, model_validator
from evals.understanding.dataset import Strict, ExpectedPlan

DATASET=Path(__file__).with_name("chains.json")

class Plan(ExpectedPlan):
    version: Literal[1]

class Turn(Strict):
    question: str=""
    action: Literal["discard"]|None=None
    status: Literal["completed","needs_clarification","denied","discarded"]
    expected: Plan|None
    base: Plan|None
    changed_fields: list[str]
    preserved_fields: list[str]
    pending_missing: list[str]
    pending_values: dict=Field(default_factory=dict)

class Chain(Strict):
    id: str
    intent: str
    group: str
    split: Literal["development","acceptance"]
    category: Literal["rules","unfamiliar","boundary"]
    identity: Literal["admin","ops"]
    turns: list[Turn]=Field(min_length=3,max_length=6)

class Dataset(Strict):
    version: Literal[1]
    annotation_status: Literal["draft","human_reviewed"]
    reviewer: str|None
    analysis_date: Literal["2018-08-31"]
    chains: list[Chain]

    @model_validator(mode="after")
    def contract(self):
        if len(self.chains)<30 or len({c.id for c in self.chains})!=len(self.chains): raise ValueError("链数量或 ID 无效")
        if sum(c.category=="unfamiliar" for c in self.chains)<10: raise ValueError("缺少陌生追问链")
        groups={}
        for c in self.chains:
            groups.setdefault(c.group,set()).add(c.split)
            previous=None
            for t in c.turns:
                if t.base!=previous: raise ValueError("标注前文不连续")
                if bool(t.question)==bool(t.action): raise ValueError("每轮须为问题或操作")
                if t.status=="completed" and t.expected is None: raise ValueError("成功缺少黄金计划")
                if t.status!="completed" and t.expected!=previous: raise ValueError("失败不能推进黄金计划")
                if set(t.changed_fields)&set(t.preserved_fields): raise ValueError("继承标注冲突")
                previous=t.expected
        if any(len(s)!=1 for s in groups.values()): raise ValueError("意图链跨分区")
        if self.annotation_status=="human_reviewed" and not self.reviewer: raise ValueError("缺少人工复核人")
        return self

def load(path=DATASET): return Dataset.model_validate(json.loads(path.read_text(encoding="utf-8")))
