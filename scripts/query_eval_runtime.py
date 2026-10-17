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
    agent.runtime.__path__ = [str(Path(os.environ['EVAL_AGENT_ROOT']) / 'runtime')]
    if args.command == 'api':
        import uvicorn
        uvicorn.run('scripts.python_analysis_eval_api:app', host='127.0.0.1', port=args.port, access_log=False)
    else:
        from scripts.python_analysis_eval_worker import main as worker
        worker()


if __name__ == '__main__':
    main()
