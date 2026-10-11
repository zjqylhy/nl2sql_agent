"""隔离镜像评测的模型转发器：真实响应透传，调用预算在出站前扣减。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from threading import Lock
import urllib.error
import urllib.request

lock = Lock()
calls = 0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path != "/healthz":
            self.send_error(404)
            return
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        global calls
        if self.path != "/chat/completions" or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + os.environ["EVAL_MODEL_KEY"]):
            self.send_error(401)
            return
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 65536:
            self.send_error(413)
            return
        body = self.rfile.read(size)
        with lock:
            if calls >= int(os.environ["EVAL_MAX_MODEL_CALLS"]):
                self.send_error(429)
                return
            calls += 1
        request = urllib.request.Request(os.environ["LLM_BASE_URL"].rstrip("/") + "/chat/completions", body,
            {"Authorization": "Bearer " + os.environ["LLM_API_KEY"], "Content-Type": "application/json"})
        try:
            # 不追随重定向，防止供应商配置意外将密钥转发至其他地址。
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args):
                    return None
            with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
                data, status = response.read(2 * 1024 * 1024), response.status
        except urllib.error.HTTPError as exc:
            data, status = b'{"error":"model_http"}', exc.code
        except Exception:
            data, status = b'{"error":"model_network"}', 502
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
