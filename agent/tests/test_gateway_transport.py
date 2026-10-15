from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ssl
from threading import Thread

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import httpx
import pytest

from runtime.model_transport import ModelTransport, ModelConfigurationError


@pytest.fixture(autouse=True)
def gateway_env(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://localhost:9443/v1/")
    monkeypatch.setenv("LLM_MODEL", "chat")
    monkeypatch.setenv("LLM_API_KEY", "fixture-gateway-key")
    for name in ("LLM_CONNECT_HOST", "LLM_CA_FILE", "SSL_CERT_FILE"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("limit", [256, 512, 2048])
def test_gateway_request_preserves_logical_identity_and_minimal_body(monkeypatch, limit):
    monkeypatch.setenv("LLM_CONNECT_HOST", "host.docker.internal")
    transport = ModelTransport.from_env()
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": []})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        transport.post(client, "只输出 JSON", "问题", limit)
    request = requests[0]
    assert str(request.url) == "https://host.docker.internal:9443/v1/chat/completions"
    assert request.headers["Host"] == "localhost:9443"
    assert request.headers["Authorization"] == "Bearer fixture-gateway-key"
    assert request.extensions["sni_hostname"] == "localhost"
    assert json.loads(request.content) == {"model": "chat", "messages": [
        {"role": "system", "content": "只输出 JSON"}, {"role": "user", "content": "问题"}], "max_tokens": limit}
    assert "fixture-gateway-key" not in repr(transport)
    with transport.client() as real_client:
        assert not real_client.follow_redirects and not real_client.trust_env
        assert real_client.timeout.read == 20


@pytest.mark.parametrize("base", ["http://localhost:9443/v1", "https://user:secret@localhost:9443/v1",
    "https://localhost:9443/v1?key=secret", "https://localhost:9443/v1#fragment", "https://", " https://localhost:9443/v1"])
def test_invalid_endpoint_fails_without_leaking_configuration(monkeypatch, base):
    monkeypatch.setenv("LLM_BASE_URL", base)
    with pytest.raises(ModelConfigurationError) as error:
        ModelTransport.from_env()
    assert str(error.value) == "invalid_model_configuration"


def test_connection_override_is_only_for_loopback(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("LLM_CONNECT_HOST", "host.docker.internal")
    with pytest.raises(ModelConfigurationError):
        ModelTransport.from_env()


@pytest.mark.parametrize("host", ["host.docker.internal:9443", "https://host.docker.internal", "host/path", "host@other"])
def test_invalid_connection_host(monkeypatch, host):
    monkeypatch.setenv("LLM_CONNECT_HOST", host)
    with pytest.raises(ModelConfigurationError):
        ModelTransport.from_env()


def test_missing_key_and_bad_ca_are_configuration_errors(monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_API_KEY")
    with pytest.raises(ModelConfigurationError):
        ModelTransport.from_env()
    monkeypatch.setenv("LLM_API_KEY", "fixture-gateway-key")
    monkeypatch.setenv("LLM_CA_FILE", str(tmp_path / "missing-ca.pem"))
    with pytest.raises(ModelConfigurationError):
        ModelTransport.from_env()


@pytest.mark.parametrize("value", ["0", "-1", "8193", "invalid"])
def test_invalid_token_budget_is_rejected(monkeypatch, value):
    monkeypatch.setenv("LLM_PLAN_MAX_TOKENS", value)
    with pytest.raises(ModelConfigurationError):
        ModelTransport.from_env()


def test_output_limits_are_explicitly_configurable(monkeypatch):
    monkeypatch.setenv("LLM_INTENT_MAX_TOKENS", "128")
    monkeypatch.setenv("LLM_PLAN_MAX_TOKENS", "384")
    config = ModelTransport.from_env()
    assert config.intent_max_tokens == 128 and config.plan_max_tokens == 384


@pytest.fixture
def tls_gateway(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture localhost")])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
        .not_valid_after(now+timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False).sign(key, hashes.SHA256()))
    ca, private = tmp_path / "ca.pem", tmp_path / "key.pem"
    ca.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            received.append((self.path, self.headers["Host"]))
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ca, private)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, ca, received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_real_tls_accepts_trusted_ca_and_rejects_wrong_identity(monkeypatch, tls_gateway):
    port, ca, received = tls_gateway
    monkeypatch.setenv("LLM_BASE_URL", f"https://localhost:{port}/v1")
    monkeypatch.setenv("LLM_CONNECT_HOST", "127.0.0.1")
    monkeypatch.setenv("LLM_CA_FILE", str(ca))
    transport = ModelTransport.from_env()
    assert transport.context.check_hostname and transport.context.verify_mode == ssl.CERT_REQUIRED
    with transport.client() as client:
        assert transport.post(client, "system", "question", 64).status_code == 200
    assert received == [("/v1/chat/completions", f"localhost:{port}")]
    monkeypatch.setenv("LLM_BASE_URL", f"https://127.0.0.1:{port}/v1")
    wrong_identity = ModelTransport.from_env()
    with wrong_identity.client() as client, pytest.raises(httpx.ConnectError):
        wrong_identity.post(client, "system", "question", 64)
    monkeypatch.delenv("LLM_CA_FILE")
    untrusted = ModelTransport.from_env()
    with untrusted.client() as client, pytest.raises(httpx.ConnectError):
        untrusted.post(client, "system", "question", 64)
    assert len(received) == 1
