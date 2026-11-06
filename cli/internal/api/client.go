// Package api is the CLI's one road to the registry: the headers every request carries, the
// edge-WAF retry and the registry's version notice.
package api

import (
	"bytes"
	"encoding/base64"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	_ "golang.org/x/crypto/x509roots/fallback" // CA roots for machines that ship none

	"github.com/superdesigndev/treg/cli/internal/buildinfo"
	"github.com/superdesigndev/treg/cli/internal/config"
	"github.com/superdesigndev/treg/cli/internal/session"
	"github.com/superdesigndev/treg/cli/internal/term"
)

const NoticeHeader = "X-Treg-Client-Notice"

// Request is one registry request. Path is relative to the registry, e.g. "/call/stripe/v1/charges".
type Request struct {
	Method  string
	Path    string
	Query   [][2]string
	Body    []byte
	Header  [][2]string
	Cookies []*http.Cookie
	// NoAuth sends no token: the registry's login routes and the installer.
	NoAuth bool
}

// Response is a whole registry answer; the CLI never prints a partial body.
type Response struct {
	Status  int
	Header  http.Header
	Body    []byte
	Cookies []*http.Cookie
	// Path is the request's registry-relative path, so output can tell a relayed call apart.
	Path string
}

func (r *Response) IsCall() bool { return strings.HasPrefix(r.Path, "/call/") }

type Client struct {
	BaseURL string
	Token   string
	Org     string
	// JSON is the global --json: it silences everything the client would print on stderr.
	JSON   bool
	Stderr io.Writer

	http          *http.Client
	noticePrinted bool
}

// RegistryURL is TREG_URL when set, else the configured base URL. TREG_URL travels with TREG_TOKEN:
// an agent's identity names its registry too.
func RegistryURL(cfg *config.Config) string {
	if u := os.Getenv("TREG_URL"); u != "" {
		return u
	}
	return cfg.BaseURL
}

// Token is TREG_TOKEN when set, else the stored token. The environment never writes the file.
func Token(cfg *config.Config) string {
	if t := os.Getenv("TREG_TOKEN"); t != "" {
		return t
	}
	return cfg.Token
}

func New(base, token, org string, json bool) *Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.DialContext = (&net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}).DialContext
	// Relayed upstreams run long and the registry's own upstream timeout is 180 s: the client must
	// outlive it so the caller sees the registry's answer, not a client-side cutoff.
	transport.ResponseHeaderTimeout = 190 * time.Second
	return &Client{
		BaseURL: base,
		Token:   token,
		Org:     org,
		JSON:    json,
		Stderr:  os.Stderr,
		http: &http.Client{
			Transport: transport,
			CheckRedirect: func(*http.Request, []*http.Request) error {
				return http.ErrUseLastResponse
			},
		},
	}
}

func (c *Client) Do(r Request) (*Response, error) {
	resp, err := c.send(r, false)
	if err != nil {
		return nil, err
	}
	c.printNotice(resp)
	if !wafBlocked(resp, r) {
		return resp, nil
	}
	if !c.JSON {
		fmt.Fprintln(c.Stderr, "  (edge WAF blocked the request body; retrying base64-encoded)")
	}
	return c.send(r, true)
}

// wafBlocked: an edge (Cloudflare) 403s a body that matches an injection signature with an HTML
// page. treg's own 403s are JSON, so only the HTML one is retried, base64-encoded, at most once.
func wafBlocked(resp *Response, r Request) bool {
	return resp.Status == http.StatusForbidden && len(r.Body) > 0 &&
		strings.Contains(strings.ToLower(resp.Header.Get("Content-Type")), "html")
}

func (c *Client) send(r Request, encoded bool) (*Response, error) {
	base, err := url.Parse(c.BaseURL)
	if err != nil || base.Host == "" {
		return nil, fmt.Errorf("the registry URL %q is not a URL", c.BaseURL)
	}
	body := r.Body
	if encoded {
		body = []byte(base64.StdEncoding.EncodeToString(r.Body))
	}
	method := r.Method
	if method == "" {
		method = http.MethodGet
	}
	req, err := http.NewRequest(method, base.Scheme+"://"+base.Host+"/", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.URL.Opaque = requestTarget(base, r.Path)
	if len(r.Query) > 0 {
		req.URL.RawQuery = encodeQuery(r.Query)
	}
	if body == nil {
		req.Body, req.ContentLength = http.NoBody, 0
	}
	c.setHeaders(req, r, encoded)
	for _, ck := range r.Cookies {
		req.AddCookie(ck)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	return &Response{Status: resp.StatusCode, Header: resp.Header, Body: data, Cookies: resp.Cookies(), Path: r.Path}, nil
}

func (c *Client) setHeaders(req *http.Request, r Request, encoded bool) {
	h := req.Header
	h.Set("Accept", "*/*")
	h.Set("User-Agent", buildinfo.UserAgent())
	h.Set("ngrok-skip-browser-warning", "1")
	h.Set("X-Treg-Client", Runtime())
	h.Set("X-Treg-Client-Version", buildinfo.Version)
	h.Set("X-Treg-Key-Protocol", "1")
	if TelemetryEnabled() {
		s := session.Current(config.Path())
		h.Set("X-Treg-Session", s.ID)
		h.Set("X-Treg-Agent", s.Agent)
	}
	if !r.NoAuth && c.Token != "" {
		h.Set("X-Treg-Token", c.Token)
		if c.Org != "" {
			h.Set("X-Treg-Org", c.Org)
		}
	}
	for _, kv := range r.Header {
		// As typed: an upstream that treats header names case-sensitively still gets the caller's spelling.
		h.Del(kv[0])
		h[kv[0]] = []string{kv[1]}
	}
	if encoded {
		h.Set("X-Treg-Body-Encoding", "base64")
	}
}

// printNotice prints the registry's word on this CLI's version on stderr, once a day per notice,
// never under --json.
func (c *Client) printNotice(resp *Response) {
	text := resp.Header.Get(NoticeHeader)
	if text == "" || c.noticePrinted || c.JSON {
		return
	}
	c.noticePrinted = true
	stamp := config.Sibling("client-notice")
	shown := time.Now().Format("2006-01-02") + " " + text
	if prev, err := os.ReadFile(stamp); err == nil && string(prev) == shown {
		return
	}
	p := term.Colors()
	fmt.Fprintf(c.Stderr, "  %s%s%s\n", p.Muted, text, p.Reset)
	_ = os.MkdirAll(config.Sibling(""), 0o700)
	_ = os.WriteFile(stamp, []byte(shown), 0o600)
}

func TelemetryEnabled() bool {
	switch strings.ToLower(os.Getenv("TREG_TELEMETRY")) {
	case "0", "false", "off":
		return false
	}
	return os.Getenv("DO_NOT_TRACK") != "1"
}

// Runtime names the coding agent this CLI runs inside, sent as X-Treg-Client for attribution only.
// Only variables a runtime sets while executing a command count: a config-location variable such
// as CODEX_HOME sits in the shell profile of anyone who installed that agent.
func Runtime() string {
	if v := strings.TrimSpace(os.Getenv("TREG_CLIENT")); v != "" {
		return v
	}
	for _, m := range [][2]string{
		{"CLAUDECODE", "claude-code"},
		{"CODEX_SANDBOX", "codex"}, {"CODEX_SANDBOX_NETWORK_DISABLED", "codex"},
		{"CURSOR_AGENT", "cursor"}, {"CURSOR_TRACE_ID", "cursor"},
		{"GEMINI_CLI", "gemini-cli"},
		{"PI_CODING_AGENT", "pi"},
		{"GITHUB_COPILOT_AGENT", "copilot"},
	} {
		if os.Getenv(m[0]) != "" {
			return m[1]
		}
	}
	return "cli"
}
