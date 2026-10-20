"""预算随任务冻结；价格单位为每百万 Token 的微货币单位。"""
import os
from pydantic import Field
from agent.runtime.catalog import StrictModel
from agent.runtime.model_budget import DEFAULT_INPUT_BYTES, LEGACY_INPUT_BYTES


class Policy(StrictModel):
    currency: str = Field(min_length=1, max_length=12)
    price_version: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=100)
    tokenizer: str
    input_price: int = Field(gt=0, le=10**9, strict=True)
    output_price: int = Field(gt=0, le=10**9, strict=True)
    max_cost: int = Field(gt=0, le=10**9, strict=True)
    max_output_tokens: int = Field(default=1024, ge=256, le=4096, strict=True)
    max_input_bytes: int = Field(default=LEGACY_INPUT_BYTES, ge=1024, le=65536, strict=True)
    model_timeout_seconds: int = Field(default=20, ge=5, le=60, strict=True)
    max_tokens: int = Field(default=30000, ge=10000, le=1000000, strict=True)
    model_interval_seconds: int = Field(default=0, ge=0, le=30, strict=True)
    strategy: str = "adaptive"

    @property
    def reserved_tokens(self):
        return self.max_input_bytes + self.max_output_tokens

    @property
    def reserved_cost(self):
        return self.cost(self.max_input_bytes, self.max_output_tokens)

    def cost(self, input_tokens, output_tokens):
        return (input_tokens * self.input_price + output_tokens * self.output_price + 999999) // 1000000

    @classmethod
    def from_env(cls):
        if os.getenv("ANALYSIS_ENABLED") != "true":
            return None
        value = cls(currency=os.environ["ANALYSIS_CURRENCY"], price_version=os.environ["ANALYSIS_PRICE_VERSION"],
            model=os.environ["LLM_MODEL"], tokenizer=os.environ["ANALYSIS_TOKENIZER"],
            input_price=int(os.environ["ANALYSIS_INPUT_PRICE_MICROS"]), output_price=int(os.environ["ANALYSIS_OUTPUT_PRICE_MICROS"]),
            max_cost=int(os.environ["ANALYSIS_MAX_COST_MICROS"]),
            max_output_tokens=int(os.getenv("ANALYSIS_MODEL_MAX_OUTPUT_TOKENS", "1024")),
            max_input_bytes=int(os.getenv('ANALYSIS_MAX_INPUT_BYTES',str(DEFAULT_INPUT_BYTES))),
            model_timeout_seconds=int(os.getenv("ANALYSIS_MODEL_TIMEOUT_SECONDS", "20")),
            max_tokens=int(os.getenv("ANALYSIS_MAX_TOKENS", "400000")),
            model_interval_seconds=int(os.getenv("ANALYSIS_MODEL_INTERVAL_SECONDS", "0")),
            strategy=os.getenv("ANALYSIS_STRATEGY", "adaptive"))
        if value.tokenizer != "byte_bpe_upper_bound" or value.strategy not in {"adaptive", "fixed"}:
            raise ValueError("未确认的 Token 预算方式或执行策略")
        if value.reserved_tokens>value.max_tokens or value.reserved_cost>value.max_cost:
            raise ValueError('任务Token或费用上限不足以预留一次模型请求，请核对输入、输出和总预算配置')
        return value
