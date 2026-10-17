"""仅独立评测使用：复用单步观测器，分析仍走真实无状态工具接口。"""
from scripts.understanding_runtime import app
from runtime.analysis_api import router

app.include_router(router)
