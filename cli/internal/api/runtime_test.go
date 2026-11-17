package api

import "testing"

func TestRuntimeNamesCopilot(t *testing.T) {
	markers := []string{"TREG_CLIENT", "CLAUDECODE", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED",
		"CURSOR_AGENT", "CURSOR_TRACE_ID", "GEMINI_CLI", "PI_CODING_AGENT", "GITHUB_COPILOT_AGENT",
		"COPILOT_CLI", "COPILOT_AGENT_SESSION_ID", "COPILOT_AGENT"}
	cases := []struct {
		env  map[string]string
		want string
	}{
		{map[string]string{}, "cli"},
		{map[string]string{"COPILOT_CLI": "1", "COPILOT_AGENT_SESSION_ID": "94be"}, "copilot-cli"},
		{map[string]string{"COPILOT_AGENT": "1"}, "copilot-vscode"},
		{map[string]string{"COPILOT_AGENT": "1", "COPILOT_CLI": "1"}, "copilot-cli"},
		{map[string]string{"COPILOT_AGENT": "1", "TREG_CLIENT": "mine"}, "mine"},
	}
	for _, c := range cases {
		for _, k := range markers {
			t.Setenv(k, c.env[k])
		}
		if got := Runtime(); got != c.want {
			t.Errorf("%v: got %q want %q", c.env, got, c.want)
		}
	}
}
