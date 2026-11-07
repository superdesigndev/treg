package session

import (
	"path/filepath"
	"testing"
)

func TestResolveMatchesThePythonCLI(t *testing.T) {
	cfg := filepath.Join(t.TempDir(), "config.json")
	claude := []Proc{{PID: 10, Name: "zsh", Started: 1}, {PID: 20, Name: "claude", Started: 99}}
	cases := []struct {
		name  string
		env   map[string]string
		procs []Proc
		want  Session
	}{
		{"explicit id", map[string]string{"TREG_SESSION_ID": "task-1"}, claude, Session{"task-1", "claude-code"}},
		{"unsafe explicit id is hashed", map[string]string{"TREG_SESSION_ID": "has space"}, nil, Session{hash("env", "has space"), "unknown"}},
		{"agent session variable", map[string]string{"CLAUDE_CODE_SESSION_ID": "s1"}, claude, Session{hash("claude-code", "s1"), "claude-code"}},
		{"agent process", map[string]string{}, claude, Session{hash("claude-code", "20:99"), "claude-code"}},
		{"codex by name", map[string]string{"CODEX_THREAD_ID": "th"}, []Proc{{PID: 5, Name: "codex-aarch64", Started: 3}}, Session{hash("codex", "th"), "codex"}},
		{"presence without a tree", map[string]string{"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s2"}, nil, Session{hash("claude-code", "s2"), "claude-code"}},
	}
	for _, c := range cases {
		if got := Resolve(cfg, c.env, c.procs); got != c.want {
			t.Errorf("%s: got %+v want %+v", c.name, got, c.want)
		}
	}
	idle := Resolve(cfg, map[string]string{}, nil)
	if again := Resolve(cfg, map[string]string{}, nil); idle != again || idle.Agent != "unknown" {
		t.Fatalf("the idle window id changed: %+v %+v", idle, again)
	}
}
