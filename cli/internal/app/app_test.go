package app

import (
	"bytes"
	"encoding/json/v2"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// lastEvent runs one invocation in a fresh config directory and returns the properties of the
// usage event it queued.
func lastEvent(t *testing.T, argv ...string) map[string]any {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("TREG_CONFIG", filepath.Join(dir, "config.json"))
	t.Setenv("TREG_URL", "http://127.0.0.1:9")
	t.Setenv("TREG_TELEMETRY", "")
	t.Setenv("DO_NOT_TRACK", "")
	t.Setenv("TREG_NO_AUTO_UPDATE", "1")
	t.Setenv("TREG_SESSION_ID", "task-1")
	if code := Main(argv, io.Discard); code != usageError {
		t.Fatalf("%v exited %d, want %d", argv, code, usageError)
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
	if event.Properties["command"] != "usage_error" {
		t.Fatalf("%v recorded %v", argv, event.Properties["command"])
	}
	return event.Properties
}

func TestAUsageErrorNamesOnlyKnownCommands(t *testing.T) {
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
		p := lastEvent(t, c.argv...)
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
