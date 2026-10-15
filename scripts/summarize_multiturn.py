"""离线检查多轮实测报告，不读取凭据、不调用模型。"""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evals.multiturn.report import inspect

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report',type=Path)
    print(json.dumps(inspect(parser.parse_args().report),ensure_ascii=False,indent=2))
