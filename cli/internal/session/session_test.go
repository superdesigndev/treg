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

// The trees and variables as Copilot CLI 1.0.112 and VS Code's Copilot agent terminal leave them.
func TestResolveNamesCopilot(t *testing.T) {
	cfg := filepath.Join(t.TempDir(), "config.json")
	copilot := []Proc{{PID: 30, Name: "bash", Started: 5}, {PID: 31, Name: "copilot", Started: 4},
		{PID: 32, Name: "node", Started: 3}, {PID: 33, Name: "claude", Started: 2}}
	cliEnv := map[string]string{"COPILOT_CLI": "1", "COPILOT_AGENT_SESSION_ID": "94be", "CLAUDECODE": "1"}
	vscode := []Proc{{PID: 40, Name: "zsh", Started: 7}, {PID: 41, Name: "code helper", Started: 6}}
	cases := []struct {
		name  string
		env   map[string]string
		procs []Proc
		want  Session
	}{
		{"copilot cli nearer than the claude that started it", cliEnv, copilot, Session{hash("copilot-cli", "94be"), "copilot-cli"}},
		{"copilot cli without a tree", map[string]string{"COPILOT_CLI": "1", "COPILOT_AGENT_SESSION_ID": "94be"}, nil, Session{hash("copilot-cli", "94be"), "copilot-cli"}},
		{"vs code agent terminal", map[string]string{"COPILOT_AGENT": "1"}, vscode, Session{hash("copilot-vscode", idleWindowID(cfg)), "copilot-vscode"}},
		{"an agent on the tree wins over the vs code mark", map[string]string{"COPILOT_AGENT": "1", "CLAUDE_CODE_SESSION_ID": "s3"},
			append([]Proc{{PID: 50, Name: "claude", Started: 8}}, vscode...), Session{hash("claude-code", "s3"), "claude-code"}},
	}
	for _, c := range cases {
		if got := Resolve(cfg, c.env, c.procs); got != c.want {
			t.Errorf("%s: got %+v want %+v", c.name, got, c.want)
		}
	}
}
