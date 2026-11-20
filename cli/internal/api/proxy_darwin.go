package api

import "os/exec"

// readSystemProxy reads macOS's proxy settings as urllib's _scproxy does: the HTTP and HTTPS
// proxies when enabled, the exceptions list and "exclude simple host names". Proxy auto-config is
// ignored there too.
func readSystemProxy() systemSettings {
	out, err := exec.Command("/usr/sbin/scutil", "--proxy").Output()
	if err != nil {
		return systemSettings{}
	}
	return parseScutil(string(out))
}
