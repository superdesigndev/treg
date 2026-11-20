package api

import (
	"fmt"
	"net/url"
	"regexp"
	"strings"
)

// The Python CLI sends through httpx, and the registry and every upstream behind `/call/` have
// only ever seen httpx's encoding. These functions reproduce it: dot segments resolved after the
// path joins the base URL, existing %XX escapes kept, and everything outside httpx's PATH_SAFE
// set percent-encoded as UTF-8.

const unreserved = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"

// httpx's PATH_SAFE beyond the unreserved characters.
const pathSafe = "!$%&'()*+,;=:@/[\\]^|"

var percentEncoded = regexp.MustCompile(`%[A-Fa-f0-9]{2}`)

func quotePath(s string) string {
	var b strings.Builder
	last := 0
	for _, m := range percentEncoded.FindAllStringIndex(s, -1) {
		b.WriteString(percentEncode(s[last:m[0]]))
		b.WriteString(s[m[0]:m[1]])
		last = m[1]
	}
	b.WriteString(percentEncode(s[last:]))
	return b.String()
}

func percentEncode(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c < 0x80 && strings.IndexByte(unreserved+pathSafe, c) >= 0 {
			b.WriteByte(c)
			continue
		}
		fmt.Fprintf(&b, "%%%02X", c)
	}
	return b.String()
}

// normalizePath drops "." and ".." segments (RFC 3986 section 5.2.4), as httpx does.
func normalizePath(path string) string {
	if !strings.Contains(path, ".") {
		return path
	}
	parts := strings.Split(path, "/")
	dotted := false
	for _, p := range parts {
		if p == "." || p == ".." {
			dotted = true
			break
		}
	}
	if !dotted {
		return path
	}
	var out []string
	for _, p := range parts {
		switch {
		case p == ".":
		case p == "..":
			if len(out) > 0 && !(len(out) == 1 && out[0] == "") {
				out = out[:len(out)-1]
			}
		default:
			out = append(out, p)
		}
	}
	return strings.Join(out, "/")
}

// encodeQuery is urllib's urlencode over pairs: order and duplicates kept, spaces as '+'.
func encodeQuery(pairs [][2]string) string {
	parts := make([]string, len(pairs))
	for i, p := range pairs {
		parts[i] = url.QueryEscape(p[0]) + "=" + url.QueryEscape(p[1])
	}
	return strings.Join(parts, "&")
}

// requestTarget joins a registry-relative path to the base URL and returns the encoded path.
func requestTarget(base *url.URL, rel string) string {
	basePath := base.EscapedPath()
	if !strings.HasSuffix(basePath, "/") {
		basePath += "/"
	}
	return quotePath(normalizePath(basePath + strings.TrimLeft(rel, "/")))
}
