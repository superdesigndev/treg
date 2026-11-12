package app

import (
	"encoding/json/v2"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/superdesigndev/treg/cli/internal/telemetry"
)

// lastEvent runs one invocation in a fresh config directory and returns the properties of the
// usage event it sent to the relay. Main flushes the queue in the background, so the event reaches
// the relay through that flush or through the one run here, never the queue file at a known moment.
func lastEvent(t *testing.T, relay string, events <-chan map[string]any, argv ...string) map[string]any {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("TREG_CONFIG", filepath.Join(dir, "config.json"))
	t.Setenv("TREG_URL", relay)
	t.Setenv("TREG_TELEMETRY", "")
	t.Setenv("DO_NOT_TRACK", "")
	t.Setenv("TREG_NO_AUTO_UPDATE", "1")
	t.Setenv("TREG_SESSION_ID", "task-1")
	if code := Main(argv, io.Discard); code != usageError {
		t.Fatalf("%v exited %d, want %d", argv, code, usageError)
	}
	telemetry.Flush(t.Context(), relay, http.DefaultTransport)
	select {
	case p := <-events:
		if p["command"] != "usage_error" {
			t.Fatalf("%v recorded %v", argv, p["command"])
		}
		return p
	case <-time.After(10 * time.Second):
		t.Fatalf("%v sent nothing", argv)
		return nil
	}
}

func newRelay(t *testing.T) (string, <-chan map[string]any) {
	events := make(chan map[string]any, 64)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		var batch struct {
			Events []struct {
				Properties map[string]any `json:"properties"`
			} `json:"events"`
		}
		body, _ := io.ReadAll(req.Body)
		_ = json.Unmarshal(body, &batch)
		for _, e := range batch.Events {
			events <- e.Properties
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(srv.Close)
	return srv.URL, events
}

func TestAUsageErrorNamesOnlyKnownCommands(t *testing.T) {
	relay, events := newRelay(t)
	for _, c := range []struct {
		argv        []string
		in, missing string
		options     string
	}{
		{[]string{"with", "--", "ls"}, "", "with", ""},
		{[]string{"claude"}, "", "with", ""},
		{[]string{"hub", "ls"}, "", "hub", ""},
		{[]string{"frobnicate"}, "", "", ""},
		{[]string{"org", "budgets"}, "org", "org_budgets", ""},
		{[]string{"org", "frobnicate"}, "org", "", ""},
		{[]string{"org", "ls", "--bogus"}, "org_ls", "", "--bogus"},
		{[]string{"org", "join"}, "org_join", "", ""},
	} {
		p := lastEvent(t, relay, events, c.argv...)
		in, _ := p["in_command"].(string)
		missing, _ := p["missing_command"].(string)
		var options []string
		if list, ok := p["options"].([]any); ok {
			for _, o := range list {
				options = append(options, o.(string))
			}
		}
		if in != c.in || missing != c.missing || strings.Join(options, ",") != c.options {
			t.Errorf("%v: in_command %q missing_command %q options %v, want %q %q %q",
				c.argv, in, missing, options, c.in, c.missing, c.options)
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
