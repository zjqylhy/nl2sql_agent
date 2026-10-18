"""同一候选 Python 平台加载指定运行内核，避免把平台差异混入问数对照。"""
import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['api', 'worker'])
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    import agent.runtime
    # 旧内核优先；候选平台新增的数据域类型由候选目录补齐，避免历史基线启动失败。
    agent.runtime.__path__ = [str(Path(os.environ['EVAL_AGENT_ROOT']) / 'runtime'),
                              str(Path(__file__).resolve().parents[1] / 'agent/runtime')]
    from agent.runtime import app as runtime
    if 'domain' not in runtime.Datasource.model_fields:
        from typing import Literal

        class OrderDatasource(runtime.Datasource):
            domain: Literal['orders'] = 'orders'

        runtime.Datasource = OrderDatasource
    if args.command == 'api':
        import uvicorn
        uvicorn.run('scripts.python_analysis_eval_api:app', host='127.0.0.1', port=args.port, access_log=False)
    else:
        from scripts.python_analysis_eval_worker import main as worker
        worker()


if __name__ == '__main__':
    main()
