// Package output prints registry answers the way the Python CLI does: stdout carries the answer,
// everything treg adds goes to stderr, and the exit status says whether the request succeeded.
package output

import (
	"bytes"
	"encoding/json/jsontext"
	"encoding/json/v2"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/ojson"
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

// USD is a micro-USD amount as money, the sign outside the dollar mark: cents from a dollar up,
// thousands grouped, and under a dollar every digit down to the micro-dollar (at least two), because
// a catalog call can cost a fraction of a cent.
func USD(micro int64) string {
	sign := ""
	if micro < 0 {
		sign, micro = "-", -micro
	}
	if micro >= 1_000_000 {
		cents := (micro + 5_000) / 10_000
		return fmt.Sprintf("%s$%s.%02d", sign, groupThousands(cents/100), cents%100)
	}
	digits := strings.TrimRight(fmt.Sprintf("%06d", micro), "0")
	for len(digits) < 2 {
		digits += "0"
	}
	return sign + "$0." + digits
}

func groupThousands(n int64) string {
	s := strconv.FormatInt(n, 10)
	var b strings.Builder
	for i, c := range s {
		if i > 0 && (len(s)-i)%3 == 0 {
			b.WriteByte(',')
		}
		b.WriteRune(c)
	}
	return b.String()
}

// IndentJSON is the body as JSON indented by two spaces, or false when it is not JSON. A UTF-8
// byte-order mark is allowed. Strings and numbers keep the upstream's spelling.
func IndentJSON(body []byte) ([]byte, bool) {
	trimmed := bytes.TrimSpace(bytes.TrimPrefix(body, []byte("\xef\xbb\xbf")))
	if len(trimmed) == 0 || !ojson.Valid(trimmed) {
		return nil, false
	}
	v := jsontext.Value(bytes.Clone(trimmed))
	if err := v.Indent(jsontext.WithIndent("  ")); err != nil {
		return nil, false
	}
	return v, true
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
	return p.refused(r)
}

// refused is what follows a refusal's body: the stderr lines that explain it, and status 1.
func (p *Printer) refused(r *api.Response) error {
	p.failureDiagnostics(r)
	body := jsonObject(r.Body)
	if r.Status == 402 && isBalanceRefusal(r, body) {
		fmt.Fprintln(p.Err, "(check the balance with `treg balance`, add funds with `treg topup`)")
	}
	if detail, ok := body["detail"]; ok && strings.Contains(pyStr(detail), "choose an org") {
		if p.OrgOverride != "" {
			fmt.Fprintf(p.Err, "(the --org value %s isn't one of your teams - see `treg org ls`)\n", quote(p.OrgOverride))
		} else {
			fmt.Fprintln(p.Err, "(no valid active team - pick one with `treg org use <slug>`; see `treg org ls`)")
		}
	}
	return &Exit{Code: 1}
}

// ShowJSON prints a value indented by two spaces.
func (p *Printer) ShowJSON(v any) {
	fmt.Fprintln(p.Out, ojson.Pretty(v))
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

func quote(s string) string { return strconv.Quote(s) }
