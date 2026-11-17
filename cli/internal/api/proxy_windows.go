package api

import (
	"golang.org/x/sys/windows/registry"
)

// readSystemProxy reads the Internet Settings key as urllib's getproxies_registry and
// proxy_bypass_registry do.
func readSystemProxy() systemSettings {
	k, err := registry.OpenKey(registry.CURRENT_USER,
		`Software\Microsoft\Windows\CurrentVersion\Internet Settings`, registry.QUERY_VALUE)
	if err != nil {
		return systemSettings{}
	}
	defer k.Close()
	enabled, _, err := k.GetIntegerValue("ProxyEnable")
	if err != nil || enabled == 0 {
		return systemSettings{}
	}
	server, _, _ := k.GetStringValue("ProxyServer")
	override, _, _ := k.GetStringValue("ProxyOverride")
	return parseRegistryProxy(server, override)
}
