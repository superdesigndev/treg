package output

import (
	"bytes"
	"encoding/base64"
	"encoding/json/jsontext"
	"encoding/json/v2"
	"fmt"
	"os"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/ojson"
	"github.com/superdesigndev/treg/cli/internal/term"
)

const (
	capacityHeader   = "X-Treg-Capacity"
	exhausted        = "exhausted"
	largeAnswerBytes = 30_000
)

// ShowCall prints a `/call/` answer: the body on stdout exactly, or under --json one envelope.
func (p *Printer) ShowCall(r *api.Response) error {
	ctype := mediaType(r.Header.Get("Content-Type"))
	if r.Status < 400 {
		p.largeAnswerHint(r)
	}
	if p.JSON {
		data, _ := json.Marshal(envelope(r, ctype), jsontext.AllowInvalidUTF8(true))
		p.Out.Write(data)
		fmt.Fprintln(p.Out)
		if r.Status >= 400 {
			return &Exit{Code: 1}
		}
		return nil
	}
	if ctype != "" && !isJSONType(ctype) && !strings.HasPrefix(ctype, "text/") {
		p.Out.Write(r.Body)
		if r.Status >= 400 {
			p.failureDiagnostics(r)
			return &Exit{Code: 1}
		}
		p.chargeLine(r)
		p.hintLine(r)
		return nil
	}
	return p.Show(r)
}

func mediaType(ctype string) string {
	t, _, _ := strings.Cut(ctype, ";")
	return strings.ToLower(strings.TrimSpace(t))
}

func isJSONType(t string) bool {
	return t == "application/json" || strings.HasSuffix(t, "+json")
}

type callEnvelope struct {
	Result any           `json:"result"`
	Treg   *ojson.Object `json:"_treg"`
}

// envelope is `treg --json call`: the provider body under `result` (JSON, text, or base64 for
// binary) and what the charge and hint lines would have said under `_treg`, in micro-USD.
func envelope(r *api.Response, ctype string) callEnvelope {
	var result any
	switch {
	case ctype == "" || isJSONType(ctype):
		body := bytes.TrimPrefix(r.Body, []byte("\xef\xbb\xbf"))
		if trimmed := bytes.TrimSpace(body); ojson.Valid(trimmed) && len(trimmed) > 0 {
			result = jsontext.Value(trimmed)
		} else {
			result = text(r.Body)
		}
	case strings.HasPrefix(ctype, "text/"):
		result = text(r.Body)
	default:
		result = map[string]string{"base64": base64.StdEncoding.EncodeToString(r.Body), "content_type": ctype}
	}
	h := r.Header
	m := ojson.NewObject()
	m.Set("http_status", r.Status)
	if saturated(r) {
		m.Set("treg_saturated", true)
		if wait, err := strconv.Atoi(h.Get("Retry-After")); err == nil && isDigits(h.Get("Retry-After")) {
			m.Set("retry_after_seconds", wait)
		}
	}
	if id := h.Get("X-Treg-Call-Id"); id != "" {
		m.Set("call_id", id)
	}
	if cost := h.Get("X-Treg-Cost-Micro"); cost != "" {
		if n, err := strconv.ParseInt(strings.TrimSpace(cost), 10, 64); err == nil {
			if h.Get("X-Treg-Async") != "" {
				m.Set("reserved_micro", n)
			} else {
				m.Set("charged_micro", n)
			}
		}
	}
	if h.Get("X-Treg-Idempotent-Replay") != "" {
		m.Set("replay", true)
	}
	if v := h.Get("X-Treg-Upstream-Failed"); v != "" {
		m.Set("upstream_failed", v)
	}
	if h.Get(capacityHeader) == exhausted {
		m.Set("capacity", exhausted)
	}
	if h.Get("X-Treg-Async") != "" {
		m.Set("async", true)
	}
	if v := h.Get("X-Treg-Hint"); v != "" {
		m.Set("hint", v)
	}
	if v := h.Get("X-Treg-Routed-Tool"); v != "" {
		m.Set("routed_tool", v)
	}
	return callEnvelope{Result: result, Treg: m}
}

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

func text(body []byte) string {
	if utf8.Valid(body) {
		return string(body)
	}
	return strings.ToValidUTF8(string(body), "�")
}

// largeAnswerHint: a large answer printed to a terminal or a pipe gets one stderr line before the
// body, so an agent whose output is cut still sees how to keep it whole.
func (p *Printer) largeAnswerHint(r *api.Response) {
	if p.Stdout == nil || len(r.Body) <= largeAnswerBytes {
		return
	}
	if !term.IsTerminal(p.Stdout) && !term.IsPipe(p.Stdout) {
		return
	}
	fmt.Fprintf(p.Err, "treg: this answer is %d KB; to keep it whole send it to a file "+
		"(> a.json 2> a.err) and read the parts you need with jq (start with jq 'keys' a.json)\n",
		len(r.Body)/1000)
	if f, ok := p.Err.(*os.File); ok {
		f.Sync()
	}
}

// chargeLine is the bill for a metered call, beside the answer. An async submission's cost header
// is a hold pending settlement, so it says "reserved".
func (p *Printer) chargeLine(r *api.Response) {
	h := r.Header
	cost := h.Get("X-Treg-Original-Cost-Micro")
	if cost == "" {
		cost = h.Get("X-Treg-Cost-Micro")
	}
	if cost == "" {
		return
	}
	micro, err := strconv.ParseInt(strings.TrimSpace(cost), 10, 64)
	if err != nil {
		return
	}
	async := h.Get("X-Treg-Async") != ""
	line := "treg: charged " + USD(micro)
	if async {
		line = "treg: reserved up to " + USD(micro) + " for async settlement"
	}
	if h.Get("X-Treg-Idempotent-Replay") != "" {
		if async {
			line += " by the original call (this is a replay - nothing new reserved)"
		} else {
			line += " by the original call (this is a replay - nothing new charged)"
		}
	}
	if id := h.Get("X-Treg-Call-Id"); id != "" {
		line += " · call id " + id
	}
	fmt.Fprintln(p.Err, line)
	if notice := accountOutNotice(r); notice != "" {
		fmt.Fprintln(p.Err, "treg: "+notice)
	} else if failed := h.Get("X-Treg-Upstream-Failed"); failed != "" {
		fmt.Fprintf(p.Err, "treg: the provider reported this request as failed (%s); the body carries no result\n", failed)
	}
}

// hintLine is the registry's optional invitation (X-Treg-Hint: review|feedback); X-Treg-Review is
// what a registry before 0.19 sends.
func (p *Printer) hintLine(r *api.Response) {
	h := r.Header
	id := h.Get("X-Treg-Call-Id")
	kind := h.Get("X-Treg-Hint")
	if _, present := h["X-Treg-Hint"]; !present && h.Get("X-Treg-Review") == "requested" {
		kind = "review"
	}
	if routed := h.Get("X-Treg-Routed-Tool"); routed != "" {
		fmt.Fprintf(p.Err, "treg: %s does this job and picks the provider, with fallback; "+
			"prefer it unless you need options only this provider has.\n", routed)
	}
	if id == "" {
		return
	}
	switch kind {
	case "review":
		fmt.Fprintf(p.Err, "treg: after using this result, run treg review %s "+
			"<useful|partly|not_useful|not_sure> [--reason \"...\"]; "+
			"omit private data, then keep going with the task.\n", id)
	case "feedback":
		fmt.Fprintf(p.Err, "treg: anything confusing or wrong about this call, even if it worked? "+
			"treg feedback submit <quality|pricing|friction|other> \"what you saw\" "+
			"--call-id %s; omit private data, then keep going with the task.\n", id)
	}
}

// saturated is treg's own 503 when its database pool is exhausted, typed by `treg_saturated`.
func saturated(r *api.Response) bool {
	if r.Status != 503 {
		return false
	}
	v, ok := jsonObject(r.Body)["treg_saturated"].(bool)
	return ok && v
}

// failureDiagnostics is one stderr line an agent can file a failure under: the status, whose
// answer it is (X-Treg-Error marks treg's own refusal; without it a call's 4xx/5xx is the
// provider's, relayed unchanged) and the call id support can look up.
func (p *Printer) failureDiagnostics(r *api.Response) {
	h := r.Header
	sat := saturated(r)
	relayed := r.IsCall() && h.Get("X-Treg-Error") == "" && !sat
	whose := "treg answered"
	switch {
	case relayed:
		whose = "the provider answered; treg relayed it unchanged"
	case r.IsCall():
		whose = "treg refused the call"
	}
	if sat {
		wait := h.Get("Retry-After")
		if wait != "" {
			whose = "treg is overloaded, not the provider; retry in " + wait + "s"
		} else {
			whose = "treg is overloaded, not the provider; retry in a moment"
		}
	}
	notice := ""
	if relayed {
		notice = accountOutNotice(r)
	}
	line := fmt.Sprintf("treg: HTTP %d", r.Status)
	if notice == "" {
		line += " - " + whose
	}
	if id := h.Get("X-Treg-Call-Id"); id != "" {
		line += "; call id " + id + " (quote it to support; `treg calls` shows the record)"
	}
	if cost := h.Get("X-Treg-Cost-Micro"); cost != "" && notice == "" {
		if micro, err := strconv.ParseInt(strings.TrimSpace(cost), 10, 64); err == nil {
			line += "; charged " + USD(micro)
		}
	}
	fmt.Fprintln(p.Err, line)
	if notice != "" {
		fmt.Fprintln(p.Err, "treg: "+notice)
	}
	if r.Status == 401 && !relayed && os.Getenv("TREG_TOKEN") != "" {
		fmt.Fprintln(p.Err, "treg: TREG_TOKEN was rejected, and it overrides `treg login`: set it to a current key")
	}
}

// accountOutNotice: treg's own provider account refused for want of credit or quota. The caller
// cannot fix it, so the notice names no vendor wallet or mechanism.
func accountOutNotice(r *api.Response) string {
	if r.Header.Get(capacityHeader) != exhausted {
		return ""
	}
	notCharged := ""
	if r.Header.Get("X-Treg-Cost-Micro") == "0" {
		notCharged = " You were not charged."
	}
	return "Sorry, this tool is temporarily unavailable on treg's side. We've been notified and " +
		"will restore it shortly." + notCharged + " You can try another tool meanwhile."
}

// isBalanceRefusal: treg refused for the team's balance, not a provider's 402, not a replay.
func isBalanceRefusal(r *api.Response, body map[string]any) bool {
	detail, _ := body["detail"].(map[string]any)
	return r.Header.Get("X-Treg-Error") != "" && r.Header.Get("X-Treg-Idempotent-Replay") == "" &&
		detail != nil && detail["error"] == "insufficient_balance"
}
