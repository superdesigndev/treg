package providers

import (
	"os"
	"strings"
	"testing"

	"github.com/superdesigndev/treg/cli/internal/pyjson"
)

// testdata/scan_env.json is the Python CLI's providers module run over sampleEnv: var_names,
// env_values, scan_env and plan_actions.
const sampleEnv = `# comment
export OPENAI_API_KEY="sk-1"
TREG_RESEND_API_KEY='re_2'
HF_TOKEN=hf
HUGGINGFACE_API_KEY=hf2
GITHUB_TOKEN=gh1
GH_TOKEN=gh2
GITHUB_CLIENT_ID=cid
GITHUB_CLIENT_SECRET=csec
GITLAB_CLIENT_ID=only
TWILIO_ACCOUNT_SID=AC1
TWILIO_AUTH_TOKEN=tw
SENTRY_AUTH_TOKEN=st
STRIPE_WEBHOOK_SECRET=whsec
POSTHOG_HOST=https://x
SECRET_KEY=dj
MYSTERY_API_KEY=m
DATABASE_URL=postgres://
TELEGRAM_BOT_TOKEN=tg
CRUSTDATA_API_KEY=cd
DEBUG=1
noequals
 SPACED_TOKEN = " v "
`

func fixture(t *testing.T) *pyjson.Object {
	t.Helper()
	raw, err := os.ReadFile("testdata/scan_env.json")
	if err != nil {
		t.Fatal(err)
	}
	v, err := pyjson.Decode(raw)
	if err != nil {
		t.Fatal(err)
	}
	return v.(*pyjson.Object)
}

func dump(v any) string { return pyjson.Dumps(v, pyjson.Options{Indent: -1}) }

func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func strs(items []string) any {
	if items == nil {
		return nil
	}
	out := make([]any, len(items))
	for i, s := range items {
		out[i] = s
	}
	return out
}

func objOrNil(o *pyjson.Object) any {
	if o == nil {
		return nil
	}
	return o
}

func TestScanEnvMatchesPython(t *testing.T) {
	want := fixture(t)
	_, catalog := Bundled()

	names, _ := want.Get("names")
	if got := dump(strs(VarNames(sampleEnv))); got != dump(names) {
		t.Errorf("names:\n%s\nwant\n%s", got, dump(names))
	}
	values := EnvValues(sampleEnv, []string{"OPENAI_API_KEY", "TREG_RESEND_API_KEY", "SPACED_TOKEN", "DEBUG"})
	wantValues := Obj(want, "values")
	for _, k := range wantValues.Keys() {
		if values[k] != wantValues.String(k) {
			t.Errorf("value %s = %q, want %q", k, values[k], wantValues.String(k))
		}
	}

	dets := ScanEnv(sampleEnv, catalog)
	var gotDets []any
	for _, d := range dets {
		headers := any(nil)
		if d.Kind == "matched" {
			headers = d.RequiredHeaders
		} else {
			headers = pyjson.NewObject()
		}
		gotDets = append(gotDets, []any{d.Kind, strs(d.Vars), nullable(d.Provider), nullable(d.BaseURL),
			objOrNil(d.Auth), nullable(d.Probe), headers, nullable(d.Note)})
	}
	wantDets, _ := want.Get("detections")
	compareRows(t, "detection", gotDets, wantDets.([]any))

	var gotActs []any
	for _, a := range PlanActions(dets) {
		gotActs = append(gotActs, []any{a.Supported, nullable(a.SecretName), nullable(a.ToolName), nullable(a.BaseURL),
			objOrNil(a.Binding), headersOrEmpty(a.RequiredHeaders), strs(a.Combine), objOrNil(a.Health), nullable(a.Reason)})
	}
	wantActs, _ := want.Get("actions")
	compareRows(t, "action", gotActs, wantActs.([]any))
}

func headersOrEmpty(o *pyjson.Object) any {
	if o == nil {
		return pyjson.NewObject()
	}
	return o
}

func compareRows(t *testing.T, label string, got, want []any) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("%d %ss, want %d:\n%s\n%s", len(got), label, len(want), dump(got), dump(want))
	}
	for i := range got {
		if g, w := dump(got[i]), dump(want[i]); g != w {
			t.Errorf("%s %d:\n got %s\nwant %s", label, i, g, w)
		}
	}
}

func TestClassifyCLI(t *testing.T) {
	_, catalog := Bundled()
	entry := func(bin string) Entry {
		for _, e := range catalog {
			if Obj(e, "cli").String("bin") == bin {
				return e
			}
		}
		t.Fatalf("no %s in the catalog", bin)
		return nil
	}
	cases := []struct {
		bin                         string
		installed, secret, loggedIn bool
		want                        Decision
	}{
		{"gh", true, true, false, Decision{Status: "ready", Tier: "server"}},
		{"gh", true, false, true, Decision{Status: "ready", Tier: "local"}},
		{"gh", true, false, false, Decision{Status: "needs_key", Env: "GH_TOKEN", Login: "gh auth login"}},
		{"openai", true, false, false, Decision{Status: "needs_key", Env: "OPENAI_API_KEY"}},
		{"gh", false, true, true, Decision{Status: "not_installed", Action: "brew install gh"}},
		{"pscale", true, false, false, Decision{Status: "needs_login", Action: "run `pscale auth login`"}},
		{"az", true, true, true, Decision{Status: "unsupported", Reason: Obj(entry("az"), "cli").String("reason")}},
	}
	for _, c := range cases {
		if got := ClassifyCLI(entry(c.bin), c.installed, c.secret, c.loggedIn); got != c.want {
			t.Errorf("%s %v/%v/%v: %+v, want %+v", c.bin, c.installed, c.secret, c.loggedIn, got, c.want)
		}
	}
}

func TestMatchSkill(t *testing.T) {
	_, catalog := Bundled()
	for name, want := range map[string]string{"GSC": "Google Search Console", "stripe-cli": "Stripe", "Google_Ads": "Google Ads", "nope": ""} {
		got := ""
		if e := MatchSkill(name, catalog); e != nil {
			got = e.String("provider")
		}
		if got != want {
			t.Errorf("MatchSkill(%s) = %q, want %q", name, got, want)
		}
	}
}

func TestLLMParse(t *testing.T) {
	text := `<think>{"resolved":[{"var":"X"}]}</think>Sure: {"resolved":[` +
		`{"var":"A_KEY","provider":"A","base_url":"https://api.a.com","auth":{"shape":"bearer"}},` +
		`{"var":"A_KEY","provider":"A2","base_url":"https://api.a2.com","auth":{"shape":"bearer"}},` +
		`{"var":"B_KEY","base_url":"http://api.b.com","auth":{"shape":"bearer"}},` +
		`{"var":"C_KEY","base_url":"https://172.20.0.1","auth":{"shape":"query"}},` +
		`{"var":"D_KEY","base_url":"https://api.d.com","auth":{"shape":"oauth2"}}]} done`
	got := LLMParse(text)
	if len(got) != 1 || got[0].String("provider") != "A" {
		t.Fatalf("got %s", dump(got))
	}
	for raw, want := range map[string]bool{"https://api.x.io/v1": true, "https://localhost.": true,
		"https://x.internal": false, "https://10.0.0.1": false, "https://172.15.0.1": true, "ftp://a.b": false} {
		if SafeBaseURL(raw) != want {
			t.Errorf("SafeBaseURL(%s) != %v", raw, want)
		}
	}
	if !strings.Contains(llmSystem, "SKIP") {
		t.Fatal("system prompt changed")
	}
}
