package api

import (
	"net/http"
	"testing"
)

const surge = `<dictionary> {
  ExceptionsList : <array> {
    0 : 192.168.0.0/16
    1 : *.local
    2 : localhost
  }
  ExcludeSimpleHostnames : 1
  HTTPEnable : 1
  HTTPPort : 6152
  HTTPProxy : 127.0.0.1
  HTTPSEnable : 1
  HTTPSPort : 6152
  HTTPSProxy : 127.0.0.1
  SOCKSEnable : 1
  SOCKSPort : 6153
  SOCKSProxy : 127.0.0.1
}`

func TestParsesMacOSSettings(t *testing.T) {
	s := parseScutil(surge)
	if s.http != "http://127.0.0.1:6152" || s.https != "http://127.0.0.1:6152" || !s.local {
		t.Fatalf("settings = %+v", s)
	}
	for host, want := range map[string]bool{
		"treg.to": false, "printer.local": true, "192.168.1.9": true, "intranet": true, "10.0.0.1": false,
	} {
		if got := s.bypasses(host); got != want {
			t.Errorf("bypasses(%s) = %v", host, got)
		}
	}
	if off := parseScutil("<dictionary> {\n  HTTPEnable : 0\n  HTTPProxy : x\n}"); off.http != "" {
		t.Fatalf("a disabled proxy was read: %+v", off)
	}
}

func TestParsesWindowsSettings(t *testing.T) {
	one := parseRegistryProxy("proxy.corp:8080", "<local>;*.corp;10.*")
	if one.http != "http://proxy.corp:8080" || one.https != "http://proxy.corp:8080" || !one.local {
		t.Fatalf("settings = %+v", one)
	}
	if !one.bypasses("git.corp") || !one.bypasses("10.1.2.3") || one.bypasses("treg.to") {
		t.Fatal("ProxyOverride was not applied")
	}
	split := parseRegistryProxy("http=a:1;https=b:2;ftp=c:3", "")
	if split.http != "http://a:1" || split.https != "http://b:2" {
		t.Fatalf("settings = %+v", split)
	}
}

func TestEnvironmentWinsAndLoopbackIsDirect(t *testing.T) {
	t.Setenv("HTTPS_PROXY", "http://env-proxy:3128")
	req, _ := http.NewRequest("GET", "https://treg.to/x", nil)
	if u, _ := Proxy(req); u == nil || u.Host != "env-proxy:3128" {
		t.Fatalf("proxy = %v", u)
	}
	t.Setenv("HTTPS_PROXY", "")
	t.Setenv("NO_PROXY", "")
	systemOnce.Do(func() {})
	system = systemSettings{https: "http://sys:1"}
	if u, _ := Proxy(req); u == nil || u.Host != "sys:1" {
		t.Fatalf("system proxy not used: %v", u)
	}
	local, _ := http.NewRequest("GET", "http://127.0.0.1:9/x", nil)
	if u, _ := Proxy(local); u != nil {
		t.Fatalf("loopback proxied through %v", u)
	}
}
