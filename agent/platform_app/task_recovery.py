"""从服务器存档和完整回执构造恢复状态，不接受模型自报断点。"""
import hashlib
from fastapi import HTTPException

from agent.runtime.analysis import canonical
from agent.runtime.task_contracts import TaskContract, validate_contract
from .resume_compatibility import compatibility

RECOVERABLE = {'model_format','invalid_arguments','missing_conditions','missing_evidence','unknown_execution'}


def recoverable(code):
    return code in RECOVERABLE or code.startswith('model_') and code not in {'model_usage','model_input','model_input_limit'}


def analysis_recovery(repo, conn, user, task, run, source):
    from .repository import binding, permitted
    if run['evidence_lifecycle']!='available' or run['graph_pruned_at'] or run['state'].get('feedback',{}).get('evidence_expired'):
        raise HTTPException(409,'详细证据已过期或正在清理，不能续跑')
    proof = compatibility(run['metadata'],binding(user,True))
    if not proof:
        raise HTTPException(409,'代码版本或数据绑定已变化，尚未通过恢复兼容验证')
    saved=run.get('recovery_checkpoint')
    if not saved:
        raise HTTPException(409,'缺少已核验的恢复快照；旧任务需先完成迁移回放')
    checkpoint_id,state=saved['checkpoint_id'],saved['state']
    spec = state.get('spec')
    contract = conn.execute('SELECT body FROM task_contracts WHERE task_id=%s AND version=%s',
                            (task['id'],task['contract_version'])).fetchone()
    if not spec or spec.get('task_type')!='analysis' or not contract or contract['body']!=spec:
        raise HTTPException(409,'流程存档与已确认约定不一致，不能续跑')
    validate_contract(TaskContract.model_validate(spec),source,user['allowed_metrics'])
    records = conn.execute("SELECT a.body,a.result_ref FROM analysis_artifacts a WHERE a.run_id=%s AND a.contract_version=%s AND a.body->>'kind'<>'report' ORDER BY a.created_at,a.result_ref",
                           (task['id'],task['contract_version'])).fetchall()
    refs=[]
    for row in records:
        art=row['body']; body={k:v for k,v in art.items() if k!='result_ref'}
        expected=hashlib.sha256(canonical(body).encode()).hexdigest()
        receipt=conn.execute("SELECT 1 FROM analysis_steps WHERE run_id=%s AND status='completed' AND output->'artifact'->>'result_ref'=%s LIMIT 1",
                              (task['id'],row['result_ref'])).fetchone()
        if (expected!=row['result_ref'] or art.get('result_ref')!=row['result_ref'] or not receipt
                or art.get('binding')!=run['binding'] or not permitted(user,art)):
            raise HTTPException(409,'完整证据的归属、权限或内容校验失败，不能续跑')
        refs.append(row['result_ref'])
    reports={r['result_ref'] for r in conn.execute("SELECT result_ref FROM analysis_artifacts WHERE run_id=%s AND body->>'kind'='report'",(task['id'],))}
    if set(state.get('refs',[]))-set(refs)-reports:
        raise HTTPException(409,'流程所依赖的完整结果已丢失，不能续跑')
    # 收尾报告不是失败来源；只核对当前失败操作及执行程序保存的硬预算失败。
    step=conn.execute('SELECT id,phase,output FROM analysis_steps WHERE run_id=%s AND id=%s',
                       (task['id'],state.get('op_id',''))).fetchone()
    failure=run['state'].get('recovery_failure') or state.get('feedback',{})
    code=failure.get('error_code','')
    if not code and step:
        failure=step['output'] or {};code=failure.get('error_code','')
    if code=='model_calls_budget':
        if state.get('status') not in {None,'decide','failed'} or state.get('pending_scope'):
            raise HTTPException(409,'模型次数耗尽，但当前不是可恢复的决策位置')
    elif not recoverable(code) or not step or not (step['output'] or {}).get('error_code'):
        raise HTTPException(409,'当前失败不能通过重新发起模型决策恢复')
    elif step['phase'] not in {'decide','tool'} or step['phase']=='tool' and code not in {'invalid_arguments','missing_conditions','missing_evidence'}:
        raise HTTPException(409,'当前工具故障不在模型决策续跑范围')
    if code!='model_calls_budget' and (step['output'] or {}).get('error_code')!=code:
        raise HTTPException(409,'流程失败原因与回执不一致，不能续跑')
    feedback={k:failure[k] for k in ('error_code','status','model_sent') if k in failure}
    feedback['message']=str(failure.get('message') or task.get('error') or '上次决策失败')[:1500]
    return dict(mode='analysis_decision',checkpoint_id=checkpoint_id,contract_version=task['contract_version'],
                failed_step_ref=step['id'] if step else None,error_code=code,valid_result_refs=refs,
                feedback=feedback,compatibility=proof,recovery_version=1)


def apply_recovery(conn, task, run, saved_state, recovery, round_no):
    if saved_state.get('applied_resume_round',0)>=round_no:
        return None
    seq=conn.execute('SELECT coalesce(max(sequence),0) n FROM analysis_steps WHERE run_id=%s',(task['id'],)).fetchone()['n']
    return dict(status='decide',sequence=max(seq,saved_state.get('sequence',0),run['state'].get('sequence',0)),
        spec=run['state']['spec'],answers=saved_state.get('answers',[]),refs=recovery['valid_result_refs'],
        feedback={**recovery['feedback'],'resume_message':'保留已有证据，从失败决策继续；不要重复已完成查询'},
        decision={},output={},op_id='',message='',pending_scope=None,retry_sql=False,repairs=0,model_retries=0,
        applied_resume_round=round_no,report_ref=None)
