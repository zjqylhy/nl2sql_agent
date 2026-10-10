package api

import (
	"net/http/httptest"
	"net/netip"
	"testing"
)

func TestForwardedAddressesRequireTrustedPeer(t *testing.T) {
	s := New(nil, Config{TrustedProxies: []netip.Prefix{netip.MustParsePrefix("172.30.0.10/32"), netip.MustParsePrefix("172.30.0.1/32")}})
	for _, c := range []struct{ peer, forwarded, want string }{
		{"198.51.100.1:1234", "1.2.3.4", "198.51.100.1"},
		{"172.30.0.10:1234", "1.2.3.4, 198.51.100.2", "198.51.100.2"},
		{"172.30.0.10:1234", "198.51.100.2, 172.30.0.1", "198.51.100.2"},
		{"172.30.0.10:1234", "invalid", "172.30.0.10"},
	} {
		r := httptest.NewRequest("POST", "/api/login", nil)
		r.RemoteAddr = c.peer
		r.Header.Set("X-Forwarded-For", c.forwarded)
		if got := s.clientIP(r); got != c.want {
			t.Fatalf("got %s want %s", got, c.want)
		}
	}
}
