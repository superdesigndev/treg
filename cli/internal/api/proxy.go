package api

import (
	"net"
	"net/http"
	"net/url"
	"os"
	"path"
	"strings"
	"sync"
)

// The Python CLI sends through httpx, which takes its proxy from urllib.request.getproxies(): the
// *_proxy environment variables when any is set, else the operating system's settings (macOS
// network preferences, the Windows Internet Settings registry key). People whose only way to the
// registry is a system proxy (Surge, ClashX, a corporate one) set nothing in the environment, so
// the binary reads the same settings. A loopback host is never proxied.

type systemSettings struct {
	http, https string
	// bypass holds the exception patterns; local bypasses dotless host names.
	bypass []string
	local  bool
}

var (
	systemOnce sync.Once
	system     systemSettings
)

// UseSystemProxy makes every client built from http.DefaultTransport choose its proxy this way.
func UseSystemProxy() {
	if t, ok := http.DefaultTransport.(*http.Transport); ok {
		t.Proxy = Proxy
	}
}

func Proxy(req *http.Request) (*url.URL, error) {
	if environmentProxied() {
		return http.ProxyFromEnvironment(req)
	}
	host := req.URL.Hostname()
	if isLoopback(host) {
		return nil, nil
	}
	systemOnce.Do(func() { system = readSystemProxy() })
	proxy := system.http
	if req.URL.Scheme == "https" {
		proxy = system.https
	}
	if proxy == "" || system.bypasses(host) {
		return nil, nil
	}
	if !strings.Contains(proxy, "://") {
		proxy = "http://" + proxy
	}
	return url.Parse(proxy)
}

func environmentProxied() bool {
	for _, kv := range os.Environ() {
		name, value, _ := strings.Cut(kv, "=")
		if value != "" && strings.HasSuffix(strings.ToLower(name), "_proxy") {
			return true
		}
	}
	return false
}

func isLoopback(host string) bool {
	if strings.EqualFold(host, "localhost") || strings.HasSuffix(strings.ToLower(host), ".localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func (s systemSettings) bypasses(host string) bool {
	host = strings.ToLower(host)
	if s.local && !strings.Contains(host, ".") {
		return true
	}
	ip := net.ParseIP(host)
	for _, pattern := range s.bypass {
		pattern = strings.ToLower(strings.TrimSpace(pattern))
		if pattern == "" {
			continue
		}
		if _, network, err := net.ParseCIDR(pattern); err == nil {
			if ip != nil && network.Contains(ip) {
				return true
			}
			continue
		}
		if ok, _ := path.Match(pattern, host); ok {
			return true
		}
		if strings.HasPrefix(pattern, ".") && strings.HasSuffix(host, pattern) {
			return true
		}
	}
	return false
}
