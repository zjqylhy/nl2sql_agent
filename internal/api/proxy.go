package api

import (
	"net"
	"net/http"
	"net/netip"
	"strings"
)

func (s *Server) trusted(address netip.Addr) bool {
	for _, prefix := range s.Config.TrustedProxies {
		if prefix.Contains(address.Unmap()) {
			return true
		}
	}
	return false
}

// 只从已配置的反向代理向左回溯，遇到第一个非可信地址即停止。
func (s *Server) clientIP(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	peer, err := netip.ParseAddr(host)
	if err != nil {
		return host
	}
	if !s.trusted(peer) {
		return peer.Unmap().String()
	}
	parts := strings.Split(r.Header.Get("X-Forwarded-For"), ",")
	if len(parts) > 16 {
		return peer.Unmap().String()
	}
	for i := len(parts) - 1; i >= 0; i-- {
		next, err := netip.ParseAddr(strings.TrimSpace(parts[i]))
		if err != nil {
			return peer.Unmap().String()
		}
		peer = next
		if !s.trusted(peer) {
			break
		}
	}
	return peer.Unmap().String()
}
