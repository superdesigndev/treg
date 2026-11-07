package commands

import (
	"bytes"
	"encoding/json/v2"
	"fmt"
	"io"
	"mime"
	"mime/multipart"
	"net/textproto"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"unicode/utf16"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/config"
	"github.com/superdesigndev/treg/cli/internal/ojson"
	"github.com/superdesigndev/treg/cli/internal/output"
)

type callFlags struct {
	method, data, file, contentType, authorizationMethod string
	query, header, upload                                []string
	await                                                bool
	timeout                                              float64
}

func callCmd(env *Env) *cobra.Command {
	var f callFlags
	cmd := &cobra.Command{
		Use:   "call <target> [path]",
		Short: "Call a tool through the proxy: `call <tool> <path>` or `call <full-url>`. Key injected server-side.",
		Example: "  treg call stripe v1/charges\n" +
			"  treg call https://api.stripe.com/v1/charges\n" +
			"  treg call posthog api/events --query limit=5\n" +
			"  treg call slack chat.postMessage --method POST --data '{\"channel\":\"C1\"}'\n" +
			"  treg call reapi.tasks.get --query id=task_01a09ddf   # a catalog id: path/query params go in --query, never in a path",
		Args: cobra.RangeArgs(1, 2),
		RunE: func(_ *cobra.Command, args []string) error {
			path := ""
			if len(args) == 2 {
				path = args[1]
			}
			return env.call(args[0], path, f)
		},
	}
	fl := cmd.Flags()
	fl.StringVar(&f.method, "method", "", "HTTP method (default: GET, or POST when --data/--file/--upload is given)")
	fl.StringArrayVarP(&f.query, "query", "p", nil, "a query param (repeatable)")
	fl.StringVar(&f.authorizationMethod, "authorization-method", "", "select an authorization method declared by the catalog endpoint")
	fl.StringVar(&f.data, "data", "", "request body (string); @PATH reads a file, @- stdin")
	fl.StringVar(&f.file, "file", "", "request body from a file")
	fl.StringVar(&f.contentType, "content-type", "", "Content-Type for the body (default: sniffed - a body that parses as JSON sends application/json)")
	fl.StringArrayVarP(&f.header, "header", "H", nil, "an extra request header (repeatable), e.g. --header 'login-customer-id: 1234567890'. Injected credentials always win.")
	fl.StringArrayVar(&f.upload, "upload", nil, "a multipart/form-data part (repeatable): NAME=@/path/to/file for a file, or NAME=value for a plain field")
	fl.BoolVar(&f.await, "await", false, "wait for an async catalog call to reach a terminal state")
	fl.Float64Var(&f.timeout, "timeout", 900, "maximum seconds to wait with --await (default: 900)")
	return cmd
}

func (e *Env) call(target, path string, f callFlags) error {
	var params [][2]string
	for _, kv := range f.query {
		k, v, ok := strings.Cut(kv, "=")
		if !ok {
			return output.Fail("--query expects K=V, got: %s", quote(kv))
		}
		params = append(params, [2]string{k, v})
	}
	body, err := callBody(f)
	if err != nil {
		return err
	}
	ctype := f.contentType
	if len(f.upload) > 0 {
		body, ctype, err = multipartBody(f.upload)
		if err != nil {
			return err
		}
	}
	if ctype == "" && body != nil && len(body) > 0 {
		if ojson.Valid(bytes.TrimSpace(body)) {
			ctype = "application/json"
		} else if lead := bytes.TrimLeft(body, " \t\r\n\f\v"); len(lead) > 0 && (lead[0] == '{' || lead[0] == '[') {
			// PowerShell strips the quotes inside a quoted argument, so '[{"k":"v"}]' arrives as
			// [{k:v}]: unlabelled garbage the provider would answer with an opaque error.
			return output.Fail("the request body is not valid JSON (%s). If your shell stripped the "+
				"quotes (PowerShell does), put the body in a file and pass --file PATH or "+
				"--data @PATH; to send it as written, pass --content-type", jsonError(body))
		}
	}
	var header [][2]string
	if ctype != "" {
		header = append(header, [2]string{"content-type", ctype})
	}
	if f.authorizationMethod != "" {
		header = append(header, [2]string{"X-Treg-Authorization-Method", f.authorizationMethod})
	}
	for _, kv := range f.header {
		name, value, ok := strings.Cut(kv, ":")
		if !ok {
			return output.Fail("--header expects 'Name: value', got: %s", quote(kv))
		}
		name = strings.TrimSpace(name)
		if name == "" {
			return output.Fail("--header needs a name before the colon, got: %s", quote(kv))
		}
		header = setHeader(header, name, strings.TrimSpace(value))
	}
	rest := strings.TrimRight(target, "/")
	e.noteTool(rest)
	if path != "" {
		rest += "/" + strings.TrimLeft(path, "/")
	}
	// An inline `?a=b` in the target or path composes with --query, inline pairs first.
	if before, inline, ok := strings.Cut(rest, "?"); ok {
		rest = before
		params = append(parseQuery(inline), params...)
	}
	method := f.method
	if method == "" {
		method = "GET"
		if body != nil {
			method = "POST"
		}
	}
	c := e.Client()
	resp, err := e.do(c, api.Request{Method: method, Path: "/call/" + rest, Query: params, Body: body, Header: header})
	if err != nil {
		return err
	}
	e.Note("status_class", fmt.Sprintf("%dxx", resp.Status/100))
	if f.await && resp.Header.Get("X-Treg-Async") != "" {
		return e.awaitTask(c, resp, f.timeout)
	}
	return e.Out.ShowCall(resp)
}

// setHeader replaces a header of the same name (case-insensitively), as a Python dict keyed by
// the typed name would only for an exact match; the later value wins either way.
func setHeader(h [][2]string, name, value string) [][2]string {
	for i := range h {
		if h[i][0] == name {
			h[i][1] = value
			return h
		}
	}
	return append(h, [2]string{name, value})
}

var endpointID = regexp.MustCompile(`^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+$`)

// noteTool records the target only when it has the shape of an endpoint id, so a URL, query or
// path typed in its place is never recorded.
func (e *Env) noteTool(target string) {
	if len(target) <= 128 && endpointID.MatchString(target) {
		e.Note("tool", target)
	} else {
		delete(e.Telemetry, "tool")
	}
}

// parseQuery is Python's parse_qsl(keep_blank_values=True).
func parseQuery(q string) [][2]string {
	var out [][2]string
	for part := range strings.SplitSeq(q, "&") {
		if part == "" {
			continue
		}
		k, v, _ := strings.Cut(part, "=")
		out = append(out, [2]string{unquotePlus(k), unquotePlus(v)})
	}
	return out
}

// unquotePlus is Python's unquote_plus: '+' is a space, a valid %XX is decoded and an invalid one
// is left as typed.
func unquotePlus(s string) string {
	s = strings.ReplaceAll(s, "+", " ")
	var b []byte
	for i := 0; i < len(s); i++ {
		if s[i] == '%' && i+2 < len(s) && isHex(s[i+1]) && isHex(s[i+2]) {
			b = append(b, unhex(s[i+1])<<4|unhex(s[i+2]))
			i += 2
			continue
		}
		b = append(b, s[i])
	}
	return string(b)
}

func isHex(c byte) bool {
	return '0' <= c && c <= '9' || 'a' <= c && c <= 'f' || 'A' <= c && c <= 'F'
}

func unhex(c byte) byte {
	switch {
	case c <= '9':
		return c - '0'
	case c <= 'F':
		return c - 'A' + 10
	}
	return c - 'a' + 10
}

// callBody is the body --file or --data names; --data @PATH and @- read it, as curl does.
func callBody(f callFlags) ([]byte, error) {
	if f.file != "" {
		raw, err := os.ReadFile(f.file)
		if err != nil {
			return nil, output.Fail("treg: --file %s: %v", f.file, err)
		}
		return withoutBOM(raw, "--file "+f.file), nil
	}
	if f.data == "" {
		return nil, nil
	}
	if !strings.HasPrefix(f.data, "@") {
		return []byte(f.data), nil
	}
	source := f.data[1:]
	if source == "-" {
		raw, err := io.ReadAll(os.Stdin)
		if err != nil {
			return nil, output.Fail("treg: --data @-: %v", err)
		}
		return withoutBOM(raw, "--data @-"), nil
	}
	path := config.ExpandUser(source)
	if info, err := os.Stat(path); err != nil || !info.Mode().IsRegular() {
		return nil, output.Fail("--data %s: no such file. `@PATH` reads the body from a file, as curl "+
			"does; to send a body that starts with '@', put it in a file and pass --file", f.data)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, output.Fail("treg: --data %s: %v", f.data, err)
	}
	return withoutBOM(raw, "--data "+f.data), nil
}

// withoutBOM: PowerShell's `>` and Out-File write UTF-16 (5.1) or a UTF-8 byte-order mark, and no
// JSON API parses either. Only a body that is JSON once decoded is rewritten, and the caller is told.
func withoutBOM(raw []byte, source string) []byte {
	var text []byte
	var name string
	switch {
	case bytes.HasPrefix(raw, []byte{0xEF, 0xBB, 0xBF}):
		text, name = raw[3:], "utf-8"
	case bytes.HasPrefix(raw, []byte{0xFF, 0xFE}):
		text, name = utf16Decode(raw[2:], false), "utf-16"
	case bytes.HasPrefix(raw, []byte{0xFE, 0xFF}):
		text, name = utf16Decode(raw[2:], true), "utf-16"
	default:
		return raw
	}
	if text == nil || !ojson.Valid(text) {
		return raw
	}
	fmt.Fprintf(os.Stderr, "treg: %s starts with a %s byte-order mark; sending it as UTF-8 JSON\n", source, name)
	return text
}

func utf16Decode(b []byte, bigEndian bool) []byte {
	if len(b)%2 != 0 {
		return nil
	}
	units := make([]uint16, len(b)/2)
	for i := range units {
		if bigEndian {
			units[i] = uint16(b[2*i])<<8 | uint16(b[2*i+1])
		} else {
			units[i] = uint16(b[2*i+1])<<8 | uint16(b[2*i])
		}
	}
	return []byte(string(utf16.Decode(units)))
}

// multipartBody builds the form for --upload, encoded eagerly so the WAF retry can resend it.
func multipartBody(parts []string) ([]byte, string, error) {
	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	for _, kv := range parts {
		name, val, ok := strings.Cut(kv, "=")
		if !ok {
			return nil, "", output.Fail("--upload expects NAME=@/path (or NAME=value), got: %s", quote(kv))
		}
		h := textproto.MIMEHeader{}
		if strings.HasPrefix(val, "@") {
			path := config.ExpandUser(val[1:])
			info, err := os.Stat(path)
			if err != nil || !info.Mode().IsRegular() {
				return nil, "", output.Fail("--upload file not found: %s", path)
			}
			data, err := os.ReadFile(path)
			if err != nil {
				return nil, "", output.Fail("--upload %s: %v", path, err)
			}
			base := filepath.Base(path)
			h.Set("Content-Disposition", fmt.Sprintf(`form-data; name="%s"; filename="%s"`, escapeQuotes(name), escapeQuotes(base)))
			ctype := mime.TypeByExtension(filepath.Ext(base))
			if ctype == "" {
				ctype = "application/octet-stream"
			}
			h.Set("Content-Type", ctype)
			pw, _ := w.CreatePart(h)
			pw.Write(data)
			continue
		}
		h.Set("Content-Disposition", fmt.Sprintf(`form-data; name="%s"`, escapeQuotes(name)))
		pw, _ := w.CreatePart(h)
		pw.Write([]byte(val))
	}
	w.Close()
	return buf.Bytes(), w.FormDataContentType(), nil
}

func escapeQuotes(s string) string {
	return strings.NewReplacer(`\`, `\\`, `"`, `\"`).Replace(s)
}

func jsonError(body []byte) string {
	var v any
	if err := json.Unmarshal(body, &v); err != nil {
		return err.Error()
	}
	return "invalid JSON"
}
