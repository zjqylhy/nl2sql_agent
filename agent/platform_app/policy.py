"""预算随任务冻结；价格单位为每百万 Token 的微货币单位。"""
import os
from pydantic import Field
from agent.runtime.catalog import StrictModel


class Policy(StrictModel):
    currency: str = Field(min_length=1, max_length=12)
    price_version: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=100)
    tokenizer: str
    input_price: int = Field(gt=0, le=10**9, strict=True)
    output_price: int = Field(gt=0, le=10**9, strict=True)
    max_cost: int = Field(gt=0, le=10**9, strict=True)
    max_output_tokens: int = Field(default=1024, ge=256, le=4096, strict=True)
    model_timeout_seconds: int = Field(default=20, ge=5, le=60, strict=True)
    strategy: str = "adaptive"

    @property
    def reserved_tokens(self):
        return 6000 + self.max_output_tokens

    @property
    def reserved_cost(self):
        return self.cost(6000, self.max_output_tokens)

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
            model_timeout_seconds=int(os.getenv("ANALYSIS_MODEL_TIMEOUT_SECONDS", "20")),
            strategy=os.getenv("ANALYSIS_STRATEGY", "adaptive"))
        if value.tokenizer != "byte_bpe_upper_bound" or value.strategy not in {"adaptive", "fixed"}:
            raise ValueError("未确认的 Token 预算方式或执行策略")
        return value
