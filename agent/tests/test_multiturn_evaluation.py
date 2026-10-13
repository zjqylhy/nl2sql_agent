"""评测器必须抓住查询成功但继承条件错误的情况。"""
import json
from copy import deepcopy
from pathlib import Path
import pytest
from evals.multiturn.dataset import load, Dataset
from evals.multiturn.scorer import score

def test_chain_partition_and_explicit_context_contract():
    data=load()
    assert len(data.chains)==30 and sum(c.category=="unfamiliar" for c in data.chains)==10
    bad=data.model_dump();bad["chains"][0]["turns"][1]["base"]=None
    with pytest.raises(ValueError): Dataset.model_validate(bad)

def test_executable_wrong_plan_and_unsafe_clarification_are_failures():
    turn=load().chains[0].turns[1].model_dump()
    wrong=deepcopy(turn["expected"]);wrong["metrics"]=["orders"]
    verdict=score(turn,{"status":"completed","result":{"plan":wrong}},{"sql_calls":1},{"plan":wrong},"controlled",[])
    assert not verdict["passed"] and "metrics" in verdict["inheritance_errors"]
    turn=load().chains[0].turns[2].model_dump()
    verdict=score(turn,{"status":"needs_clarification"},{"sql_calls":1},{"plan":turn["expected"]},"controlled",[])
    assert not verdict["passed"] and verdict["unsafe_execution"]
