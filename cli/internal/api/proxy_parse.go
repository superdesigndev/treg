package api

import "strings"

// parseScutil reads `scutil --proxy` output: the HTTP and HTTPS proxies when enabled, the
// exceptions list and "exclude simple host names".
func parseScutil(out string) systemSettings {
	values := map[string]string{}
	var exceptions []string
	inExceptions := false
	for _, line := range strings.Split(out, "\n") {
		line = strings.TrimSpace(line)
		key, value, ok := strings.Cut(line, " : ")
		switch {
		case inExceptions && line == "}":
			inExceptions = false
		case inExceptions && ok:
			exceptions = append(exceptions, value)
		case ok && key == "ExceptionsList":
			inExceptions = true
		case ok:
			values[key] = value
		}
	}
	proxy := func(kind string) string {
		if values[kind+"Enable"] != "1" || values[kind+"Proxy"] == "" {
			return ""
		}
		if port := values[kind+"Port"]; port != "" {
			return "http://" + values[kind+"Proxy"] + ":" + port
		}
		return "http://" + values[kind+"Proxy"]
	}
	return systemSettings{http: proxy("HTTP"), https: proxy("HTTPS"), bypass: exceptions,
		local: values["ExcludeSimpleHostnames"] == "1"}
}

// parseRegistryProxy reads Windows' ProxyServer ("host:port", or "http=host:port;https=...") and
// ProxyOverride ("<local>;*.corp;10.*") values the way urllib does.
func parseRegistryProxy(server, override string) systemSettings {
	var s systemSettings
	server = strings.TrimSpace(server)
	if server == "" {
		return s
	}
	if strings.Contains(server, "=") {
		for _, part := range strings.Split(server, ";") {
			scheme, address, ok := strings.Cut(strings.TrimSpace(part), "=")
			if !ok || address == "" {
				continue
			}
			switch strings.ToLower(scheme) {
			case "http":
				s.http = withScheme(address)
			case "https":
				s.https = withScheme(address)
			}
		}
	} else {
		s.http, s.https = withScheme(server), withScheme(server)
	}
	for _, p := range strings.Split(override, ";") {
		if p = strings.TrimSpace(p); p == "<local>" {
			s.local = true
		} else if p != "" {
			s.bypass = append(s.bypass, p)
		}
	}
	return s
}

func withScheme(address string) string {
	if strings.Contains(address, "://") {
		return address
	}
	return "http://" + address
}
