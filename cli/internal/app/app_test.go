package app

import (
	"bytes"
	"encoding/json/v2"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"

	"github.com/superdesigndev/treg/cli/internal/buildinfo"
	"github.com/superdesigndev/treg/cli/internal/update"
)

// lastEvent runs one invocation that ends in a usage error, in a fresh config directory, and
// returns the properties of the usage event it queued.
func lastEvent(t *testing.T, argv ...string) map[string]any {
	t.Helper()
	_, p := queuedEvent(t, "http://127.0.0.1:9", usageError, argv...)
	if p["command"] != "usage_error" {
		t.Fatalf("%v recorded %v", argv, p["command"])
	}
	return p
}

// queuedEvent runs one invocation against registry in a fresh config directory, and returns the
// name and properties of the last usage event it queued.
func queuedEvent(t *testing.T, registry string, wantCode int, argv ...string) (string, map[string]any) {
	t.Helper()
	flush := startFlush
	startFlush = func(string, http.RoundTripper) {}
	t.Cleanup(func() { startFlush = flush })
	dir := t.TempDir()
	t.Setenv("TREG_CONFIG", filepath.Join(dir, "config.json"))
	t.Setenv("TREG_URL", registry)
	t.Setenv("TREG_TELEMETRY", "")
	t.Setenv("DO_NOT_TRACK", "")
	t.Setenv("TREG_NO_AUTO_UPDATE", "1")
	t.Setenv("TREG_SESSION_ID", "task-1")
	if code := Main(argv, io.Discard); code != wantCode {
		t.Fatalf("%v exited %d, want %d", argv, code, wantCode)
	}
	raw, err := os.ReadFile(filepath.Join(dir, "telemetry-queue.jsonl"))
	if err != nil {
		t.Fatalf("%v queued nothing: %v", argv, err)
	}
	lines := bytes.Split(bytes.TrimSpace(raw), []byte("\n"))
	var event struct {
		Event      string         `json:"event"`
		Properties map[string]any `json:"properties"`
	}
	if err := json.Unmarshal(lines[len(lines)-1], &event); err != nil {
		t.Fatal(err)
	}
	return event.Event, event.Properties
}

func TestABackgroundUpdateRecordsHowItEnded(t *testing.T) {
	defer func(v string) { buildinfo.Version = v }(buildinfo.Version)
	for _, c := range []struct {
		build, answer string
		code          int
		want          map[string]any
	}{
		{"1.0.0", "502", 1, map[string]any{"outcome": "check_failed", "detail": "http_502", "from_version": "1.0.0"}},
		{"9.0.0", "1.2.3", 0, map[string]any{"outcome": "up_to_date", "to_version": "1.2.3"}},
		{"1.0.0", "1.2.3", 1, map[string]any{"outcome": "failed", "reason": "no_key", "to_version": "1.2.3"}},
	} {
		buildinfo.Version = c.build
		registry := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if c.answer == "502" {
				w.WriteHeader(502)
				return
			}
			archive := update.ArchiveName(c.answer, runtime.GOOS, runtime.GOARCH)
			fmt.Fprintf(w, `{"version":%q,"archive":%q,"url":"http://x.invalid/%s"}`, c.answer, archive, archive)
		}))
		name, p := queuedEvent(t, registry.URL, c.code, "__update")
		registry.Close()
		if name != "cli_update_completed" {
			t.Fatalf("%s: queued %s", c.answer, name)
		}
		for k, v := range c.want {
			if p[k] != v {
				t.Errorf("%s on %s: %s = %v, want %v (event %v)", c.answer, c.build, k, p[k], v, p)
			}
		}
	}
}

func TestACallThatGotNoAnswerSaysWhy(t *testing.T) {
	for _, c := range []struct {
		argv []string
		want string
	}{
		{[]string{"call", "tikhub.tiktok.user.profile", "--query", "no-equals-sign"}, "bad_input"},
		{[]string{"call", "tikhub.tiktok.user.profile", "--data", "@/nonexistent/body.json"}, "bad_input"},
		{[]string{"call", "tikhub.tiktok.user.profile"}, "connection_refused"},
	} {
		_, p := queuedEvent(t, "http://127.0.0.1:9", 1, c.argv...)
		if p["command"] != "call" || p["error"] != c.want || p["status_class"] != nil {
			t.Errorf("%v: command %v error %v status_class %v, want error %s",
				c.argv, p["command"], p["error"], p["status_class"], c.want)
		}
	}
}

func TestAUsageErrorNamesOnlyCommandWords(t *testing.T) {
	for _, c := range []struct {
		argv                 []string
		in, missing, unknown string
		options              string
	}{
		{[]string{"with", "--", "ls"}, "", "with", "", ""},
		{[]string{"claude"}, "", "with", "", ""},
		{[]string{"hub", "ls"}, "", "hub", "", ""},
		{[]string{"frobnicate"}, "", "", "frobnicate", ""},
		{[]string{"org", "budgets"}, "org", "org_budgets", "", ""},
		{[]string{"org", "frobnicate"}, "org", "", "frobnicate", ""},
		{[]string{"skill", "frob"}, "skill", "", "frob", ""},
		{[]string{"org", "ls", "--bogus"}, "org_ls", "", "", "--bogus"},
		{[]string{"org", "join"}, "org_join", "", "", ""},
		{[]string{"whoami", "acme-corp"}, "whoami", "", "", ""},
		{[]string{"sk-live-4f9a"}, "", "", "", ""},
		{[]string{"Search"}, "", "", "", ""},
		{[]string{"/tmp/notes"}, "", "", "", ""},
		{[]string{"abcdefghijklmnopqrstuvwxy"}, "", "", "", ""},
	} {
		p := lastEvent(t, c.argv...)
		in, _ := p["in_command"].(string)
		missing, _ := p["missing_command"].(string)
		unknown, _ := p["unknown_command"].(string)
		var options []string
		if list, ok := p["options"].([]any); ok {
			for _, o := range list {
				options = append(options, o.(string))
			}
		}
		if in != c.in || missing != c.missing || unknown != c.unknown || strings.Join(options, ",") != c.options {
			t.Errorf("%v: in_command %q missing_command %q unknown_command %q options %v, want %q %q %q %q",
				c.argv, in, missing, unknown, options, c.in, c.missing, c.unknown, c.options)
		}
	}
}

func TestPopOrgKeepsTheSlugAndTheRest(t *testing.T) {
	for _, c := range []struct {
		in   []string
		org  string
		rest []string
	}{
		{[]string{"--org", "beta-team", "org", "ls"}, "beta-team", []string{"org", "ls"}},
		{[]string{"call", "x", "--org=acme"}, "acme", []string{"call", "x"}},
		{[]string{"whoami"}, "", []string{"whoami"}},
	} {
		org, rest, err := popOrg(append([]string(nil), c.in...))
		if err != nil || org != c.org || !reflect.DeepEqual(rest, c.rest) {
			t.Errorf("popOrg(%v) = %q %v %v", c.in, org, rest, err)
		}
	}
	if _, _, err := popOrg([]string{"call", "--org"}); err == nil {
		t.Error("a trailing --org was accepted")
	}
}

// compareRegistry answers every call with a compare invitation and records what the CLI sent.
func compareRegistry(t *testing.T) (*httptest.Server, *[]http.Header, *[]http.Header) {
	t.Helper()
	var calls, reads []http.Header
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasPrefix(r.URL.Path, "/call/"):
			calls = append(calls, r.Header.Clone())
			w.Header().Set("Content-Type", "application/json")
			w.Header().Set("X-Treg-Call-Id", "c_1")
			w.Header().Set("X-Treg-Hint", "compare")
			w.Header().Set("X-Treg-Hint-Text", "treg compare shows both")
			fmt.Fprint(w, `{"ok":true}`)
		case r.URL.Path == "/comparisons/next":
			reads = append(reads, r.Header.Clone())
			fmt.Fprint(w, `{"comparison_id":7,"answer_1":"a","answer_2":"b","how_to_vote":"treg compare 7 1|2"}`)
		default:
			w.WriteHeader(404)
		}
	}))
	t.Cleanup(srv.Close)
	return srv, &calls, &reads
}

func TestAComparisonIsReadAsTheTeamTheCallUsed(t *testing.T) {
	srv, calls, reads := compareRegistry(t)
	t.Setenv("TREG_TOKEN", "tok-a")
	queuedEvent(t, srv.URL, 0, "--org", "team-b", "call", "example.search")
	if got := (*calls)[0].Get("X-Treg-Hints"); got != "review,mark,feedback,compare" {
		t.Fatalf("call declared %q", got)
	}
	if len(*reads) != 1 || (*reads)[0].Get("X-Treg-Org") != "team-b" || (*reads)[0].Get("X-Treg-Token") != "tok-a" {
		t.Fatalf("comparison read as %v", *reads)
	}
}

func TestACallThatCannotShowAComparisonDoesNotAskForOne(t *testing.T) {
	for _, argv := range [][]string{
		{"call", "example.search", "--header", "X-Treg-Token: tok-b"},
		{"call", "example.search", "--header", "x-treg-org: team-b"},
		{"call", "example.search", "--await"},
	} {
		srv, calls, reads := compareRegistry(t)
		t.Setenv("TREG_TOKEN", "tok-a")
		queuedEvent(t, srv.URL, 0, argv...)
		if got := (*calls)[0].Get("X-Treg-Hints"); got != "review,mark,feedback" {
			t.Errorf("%v declared %q", argv, got)
		}
		if len(*reads) != 0 {
			t.Errorf("%v read a comparison", argv)
		}
	}
}
