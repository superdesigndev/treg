// Package output prints registry answers the way the Python CLI does: stdout carries the answer,
// everything treg adds goes to stderr, and the exit status says whether the request succeeded.
package output

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/term"
)

// Exit stops the command with a status after its output is written.
type Exit struct {
	Code    int
	Message string
}

func (e *Exit) Error() string { return e.Message }

// Fail is a refusal the CLI itself decides: the message goes to stderr and the status is 1.
func Fail(format string, a ...any) error {
	return &Exit{Code: 1, Message: fmt.Sprintf(format, a...)}
}

// Printer carries the streams and the invocation facts output depends on.
type Printer struct {
	Out, Err io.Writer
	// Stdout is the real stdout, for the large-answer hint that depends on what it is attached to.
	Stdout *os.File
	JSON   bool
	// OrgOverride is the global --org, named when the registry refuses the team.
	OrgOverride string
}

func NewPrinter(json bool, org string) *Printer {
	return &Printer{Out: os.Stdout, Err: os.Stderr, Stdout: os.Stdout, JSON: json, OrgOverride: org}
}

// USD is Python's f"${micro / 1_000_000:g}".
func USD(micro int64) string {
	return "$" + strconv.FormatFloat(float64(micro)/1_000_000, 'g', 6, 64)
}

// IndentJSON is the body as JSON indented by two spaces, or false when it is not JSON. A UTF-8
// byte-order mark is allowed, as Python's json.loads allows it. Strings and numbers keep the
// upstream's spelling.
func IndentJSON(body []byte) ([]byte, bool) {
	trimmed := bytes.TrimSpace(bytes.TrimPrefix(body, []byte("\xef\xbb\xbf")))
	if len(trimmed) == 0 || !json.Valid(trimmed) {
		return nil, false
	}
	var buf bytes.Buffer
	if err := json.Indent(&buf, trimmed, "", "  "); err != nil {
		return nil, false
	}
	return buf.Bytes(), true
}

// Show prints a registry answer: indented JSON, or the text as it came. A success also gets the
// charge and hint lines; a failure gets the diagnostics line and status 1.
func (p *Printer) Show(r *api.Response) error {
	if pretty, ok := IndentJSON(r.Body); ok {
		p.Out.Write(pretty)
	} else {
		p.Out.Write(r.Body)
	}
	fmt.Fprintln(p.Out)
	if r.Status < 400 {
		p.chargeLine(r)
		p.hintLine(r)
		return nil
	}
	p.failureDiagnostics(r)
	body := jsonObject(r.Body)
	if r.Status == 402 && isBalanceRefusal(r, body) {
		fmt.Fprintln(p.Err, "(check the balance with `treg balance`, add funds with `treg topup`)")
	}
	if detail, ok := body["detail"]; ok && strings.Contains(pyStr(detail), "choose an org") {
		if p.OrgOverride != "" {
			fmt.Fprintf(p.Err, "(the --org value %s isn't one of your teams - see `treg org ls`)\n", pyRepr(p.OrgOverride))
		} else {
			fmt.Fprintln(p.Err, "(no valid active team - pick one with `treg org use <slug>`; see `treg org ls`)")
		}
	}
	return &Exit{Code: 1}
}

// ShowJSON prints a value the way Python's json.dumps(value, indent=2) lays it out.
func (p *Printer) ShowJSON(v any) {
	data, _ := MarshalIndent(v)
	p.Out.Write(data)
	fmt.Fprintln(p.Out)
}

func MarshalIndent(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(buf.Bytes(), []byte("\n")), nil
}

func jsonObject(body []byte) map[string]any {
	var v map[string]any
	if json.Unmarshal(bytes.TrimPrefix(body, []byte("\xef\xbb\xbf")), &v) != nil {
		return nil
	}
	return v
}

// pyStr is Python's str() of a decoded JSON value, enough for a substring test.
func pyStr(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	data, _ := json.Marshal(v)
	return string(data)
}

// pyRepr is Python's repr() of a plain string.
func pyRepr(s string) string {
	quote := "'"
	if strings.Contains(s, "'") && !strings.Contains(s, `"`) {
		quote = `"`
	}
	r := strings.ReplaceAll(s, `\`, `\\`)
	if quote == "'" {
		r = strings.ReplaceAll(r, "'", `\'`)
	}
	return quote + r + quote
}

func colors() term.Palette { return term.Colors() }
