"""网关 HTTP 配置；容器连接地址与 HTTPS 证书身份可以分开。"""
from dataclasses import dataclass, field
import asyncio
import ipaddress
import os
import re
import ssl

import certifi
import httpx


class ModelConfigurationError(ValueError):
    pass


@dataclass
class ModelTransport:
    model: str
    url: httpx.URL
    host: str
    key: str = field(repr=False)
    context: ssl.SSLContext = field(repr=False)
    server_name: str | None = None
    intent_max_tokens: int = 256
    plan_max_tokens: int = 512

    @classmethod
    def from_env(cls):
        base = os.getenv("LLM_BASE_URL", "")
        model, key = os.getenv("LLM_MODEL", ""), os.getenv("LLM_API_KEY", "")
        try:
            logical = httpx.URL(base.rstrip("/"))
            if (base != base.strip() or logical.scheme != "https" or not logical.host
                    or logical.username or logical.password or logical.query or logical.fragment
                    or not model.strip() or not key.strip()):
                raise ValueError("invalid_model_configuration")
            target = httpx.URL(str(logical).rstrip("/") + "/chat/completions")
            connect_host = os.getenv("LLM_CONNECT_HOST", "")
            server_name = None
            if connect_host:
                # 仅本机隧道需要连接覆盖，避免误改生产 DNS 身份。
                if logical.host not in {"localhost", "127.0.0.1", "::1"}:
                    raise ValueError("loopback_endpoint_required")
                try:
                    ipaddress.ip_address(connect_host)
                except ValueError:
                    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", connect_host):
                        raise ValueError("invalid_connect_host")
                target = target.copy_with(host=connect_host)
                server_name = logical.host
            ca = os.getenv("LLM_CA_FILE") or os.getenv("SSL_CERT_FILE") or certifi.where()
            limits = [int(os.getenv(name, default)) for name, default in (
                ("LLM_INTENT_MAX_TOKENS", "256"), ("LLM_PLAN_MAX_TOKENS", "512"))]
            if any(limit < 1 or limit > 8192 for limit in limits):
                raise ValueError("invalid_output_limit")
            context = ssl.create_default_context(cafile=ca)
            return cls(model, target, logical.netloc.decode("ascii"), key, context, server_name, *limits)
        except (ValueError, httpx.InvalidURL, OSError) as exc:
            # 配置错误可能包含完整 URL/文件路径，不向调用方泄露原异常。
            raise ModelConfigurationError("invalid_model_configuration") from exc

    def client(self):
        return httpx.Client(verify=self.context, timeout=20, follow_redirects=False, trust_env=False)

    def bounded_client(self):
        return DeadlineClient(self.context)

    def post(self, client, prompt: str, question: str, max_tokens: int):
        # 网关当前没有支持强制 JSON 模式的路由，返回内容仍由业务层严格校验。
        return client.post(self.url,
            headers={"Authorization": "Bearer " + self.key, "Host": self.host},
            extensions={"sni_hostname": self.server_name} if self.server_name else {},
            json={"model": self.model, "messages": [{"role": "system", "content": prompt},
                {"role": "user", "content": question}], "max_tokens": max_tokens})


class DeadlineClient:
    """Agent 调用使用整体截止时间，避免分段读超时不断延长一次决策。"""
    def __init__(self, context, timeout=20):
        self.context, self.timeout = context, timeout

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        async def send():
            async with httpx.AsyncClient(verify=self.context, timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
                return await client.post(*args, **kwargs)
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if os.name == "nt" else asyncio.new_event_loop) as runner:
            try:
                return runner.run(asyncio.wait_for(send(), timeout=self.timeout))
            except TimeoutError:
                raise httpx.ReadTimeout("analysis_model_deadline") from None
