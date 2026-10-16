import ipaddress
from starlette.requests import Request
from agent.platform_app.api import client_address, same_origin


def test_forwarded_identity_requires_trusted_peer_and_rightmost_untrusted_client():
    def request(peer, forwarded):
        return Request({"type": "http", "client": (peer, 123), "headers": [(b"x-forwarded-for", forwarded.encode())]})
    trusted = [ipaddress.ip_network("172.30.0.10/32")]
    assert client_address(request("198.51.100.3", "203.0.113.9"), trusted) == "198.51.100.3"
    assert client_address(request("172.30.0.10", "203.0.113.9, 198.51.100.3"), trusted) == "198.51.100.3"
    assert client_address(request("172.30.0.10", "not-an-ip"), trusted) == "172.30.0.10"
    assert same_origin("https://app.example", "https://app.example")
    assert not same_origin("https://app.example@evil.example", "https://app.example")
    assert not same_origin("https://app.example/path", "https://app.example")
