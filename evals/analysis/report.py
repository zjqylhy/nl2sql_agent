"""按完整任务统计；未知费用和未执行的评测不补零。"""
import math
import random


def summary(results):
    supported = [r for r in results if r.get("expected_complete")]
    successes = sum(bool(r.get("complete_success")) for r in supported)
    latencies = sorted(r["duration_ms"] for r in results if r.get("duration_ms") is not None)
    def percentile(p):
        return latencies[max(0, math.ceil(len(latencies)*p)-1)] if latencies else None
    proposed = sum(r.get("tool_proposals", 0) for r in results)
    correct = sum(r.get("correct_parameters", 0) for r in results)
    unknown = sum(r.get("unknown_calls", 0) for r in results)
    extra = [r.get("extra_calls") for r in results if r.get("extra_calls") is not None]
    upper = [r.get("calls_above_minimum") for r in results if r.get("calls_above_minimum") is not None]
    known_cost = sum(r.get("known_cost_micros", 0) for r in results)
    reserved = [r.get('reserved_and_known_cost_micros') for r in results]
    categories={}
    for row in results:
        category=categories.setdefault(row.get('category','unknown'),{'total':0,'behavior_correct':0,'supported':0,'complete_successes':0})
        category['total']+=1
        category['behavior_correct']+=bool(row.get('behavior_correct'))
        category['supported']+=bool(row.get('expected_complete'))
        category['complete_successes']+=bool(row.get('complete_success'))
    return {"supported_tasks": len(supported), "complete_successes": successes, "categories":categories,
            "complete_success_rate": successes/len(supported) if supported else None,
            "behavior_correct": sum(bool(r.get("behavior_correct")) for r in results), "all_tasks": len(results),
            "tool_proposals": proposed, "correct_parameters": correct, "parameter_correct_rate": correct/proposed if proposed else None,
            "extra_calls_total": sum(extra) if extra else None,
            "calls_above_minimum_total": sum(upper) if upper else None,
            "latency_ms": {"p50": percentile(.5), "p95": percentile(.95), "samples": len(latencies)},
            "known_tokens": sum(r.get("known_tokens", 0) for r in results), "unknown_calls": unknown,
            "model_calls": sum(r.get('model_calls') or 0 for r in results),
            "sql_calls": sum(r.get('sql_calls') or 0 for r in results),
            "known_cost_micros": known_cost,
            "known_cost_per_complete_success": known_cost/successes if successes else None,
            "reserved_and_known_cost_micros": sum(reserved) if reserved and all(v is not None for v in reserved) else None,
            "total_cost_known": unknown == 0 and all(r.get("cost_known", False) for r in results),
            "not_run": sum(r.get("status") == "not_run" for r in results)}


def paired_difference(left, right):
    """按意图组重采样，避免把同组改写和重复运行当作独立样本。"""
    a={(r['id'],r['repeat']):r for r in left if r.get('expected_complete')}
    b={(r['id'],r['repeat']):r for r in right if r.get('expected_complete')}
    if a.keys()!=b.keys() or not a:
        return {'available':False,'reason':'unpaired_or_empty'}
    groups={}
    for key in a:
        groups.setdefault(a[key]['intent_group'],[]).append(int(bool(b[key].get('complete_success')))-int(bool(a[key].get('complete_success'))))
    means=[sum(v)/len(v) for v in groups.values()]
    rng=random.Random(20261017)
    samples=sorted(sum(rng.choices(means,k=len(means)))/len(means) for _ in range(2000))
    return {'available':True,'paired_tasks':len(a),'intent_groups':len(groups),'difference':sum(means)/len(means),
            'cluster_bootstrap_95_interval':[samples[49],samples[1949]],'seed':20261017}
