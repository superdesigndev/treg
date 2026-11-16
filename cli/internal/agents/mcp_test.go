package agents

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/superdesigndev/treg/cli/internal/ojson"
)

// The expected files are what the Python CLI's mcp_install writes for the same input.

func writeCase(t *testing.T, text string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "cfg")
	if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func read(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func cursorEntry(url, token string) *ojson.Object {
	return ojson.Ordered("url", url, "headers", bearer(token))
}

func TestJSONMergeKeepsEverythingElse(t *testing.T) {
	path := writeCase(t, `{"mcpServers": {"other": {"url": "x"}, "treg": {"old": 1}}, "theme": "dark", "n": 1.50, "u": "é"}`)
	status, _ := writeJSONAgent(path, "mcpServers", "treg", cursorEntry("https://treg.to/mcp/", "tok-ü"), false)
	want := "{\n  \"mcpServers\": {\n    \"other\": {\n      \"url\": \"x\"\n    },\n    \"treg\": {\n      \"url\": \"https://treg.to/mcp/\",\n" +
		"      \"headers\": {\n        \"Authorization\": \"Bearer tok-ü\"\n      }\n    }\n  },\n  \"theme\": \"dark\",\n  \"n\": 1.50,\n  \"u\": \"é\"\n}\n"
	if status != OK || read(t, path) != want {
		t.Fatalf("status %s, got:\n%s", status, read(t, path))
	}
	if runtime.GOOS != "windows" {
		if info, _ := os.Stat(path); info.Mode().Perm() != 0o600 {
			t.Errorf("mode %v, want 0600", info.Mode().Perm())
		}
	}
}

func TestJSONMergeReplacesANonObjectServerList(t *testing.T) {
	path := writeCase(t, `{"mcpServers": [1, 2], "a": true}`)
	writeJSONAgent(path, "mcpServers", "treg", cursorEntry("https://treg.to/mcp/", "tok-ü"), false)
	want := "{\n  \"mcpServers\": {\n    \"treg\": {\n      \"url\": \"https://treg.to/mcp/\",\n      \"headers\": {\n" +
		"        \"Authorization\": \"Bearer tok-ü\"\n      }\n    }\n  },\n  \"a\": true\n}\n"
	if got := read(t, path); got != want {
		t.Fatalf("got:\n%s", got)
	}
}

func TestJSONMergeRefusesANonObjectFile(t *testing.T) {
	path := writeCase(t, `[1]`)
	if status, detail := writeJSONAgent(path, "mcp", "treg", cursorEntry("u", "t"), false); status != Failed || detail != path+" is not a JSON object" {
		t.Fatalf("%s %s", status, detail)
	}
	if read(t, path) != "[1]" {
		t.Fatal("a refused file was rewritten")
	}
}

func TestJSONOAuthDropsTheHeader(t *testing.T) {
	path := filepath.Join(t.TempDir(), "new", "mcp.json")
	writeJSONAgent(path, "mcpServers", "treg", cursorEntry("https://treg.to/mcp/", "t"), true)
	if got := read(t, path); got != "{\n  \"mcpServers\": {\n    \"treg\": {\n      \"url\": \"https://treg.to/mcp/\"\n    }\n  }\n}\n" {
		t.Fatalf("got:\n%s", got)
	}
}

func TestCodexCutsTheOldTableAndItsTwin(t *testing.T) {
	path := writeCase(t, "model = \"o3\"\n\n[mcp_servers.treg]\nurl = \"https://old/mcp/\"\nbearer_token_env_var = \"TREG_TOKEN\"\n\n"+
		"[mcp_servers.twin]\nurl = \"https://treg.to/mcp/\"\n\n[profiles.x]\na = 1\n")
	status, detail := writeCodex(path, "treg", "https://treg.to/mcp/", "tok-1", false)
	want := "model = \"o3\"\n\n[profiles.x]\na = 1\n\n[mcp_servers.treg]\nurl = \"https://treg.to/mcp/\"\n" +
		"http_headers = { \"Authorization\" = \"Bearer tok-1\" }\n"
	if status != OK || read(t, path) != want {
		t.Fatalf("%s %s, got:\n%s", status, detail, read(t, path))
	}
}

func TestCodexReadsCRLFAsPythonDoes(t *testing.T) {
	path := writeCase(t, "model = \"o3\"\r\n\r\n[mcp_servers.treg]\r\nurl = \"https://old/mcp/\"\r\n\r\n[x]\r\nb = 2\r\n")
	writeCodex(path, "treg", "https://treg.to/mcp/", "tok-1", true)
	if got := read(t, path); got != "model = \"o3\"\n\n[x]\nb = 2\n\n[mcp_servers.treg]\nurl = \"https://treg.to/mcp/\"\n" {
		t.Fatalf("got:\n%q", got)
	}
}

func TestCodexRefusesASurvivingSubTable(t *testing.T) {
	old := "[mcp_servers.treg]\nurl = \"a\"\n\n[mcp_servers.treg.env]\nK = \"v\"\n"
	path := writeCase(t, old)
	status, detail := writeCodex(path, "treg", "https://treg.to/mcp/", "tok-1", false)
	if status != Failed || !strings.Contains(detail, "a sub-table of [mcp_servers.treg] survived") || read(t, path) != old {
		t.Fatalf("%s %s", status, detail)
	}
}

func TestCodexRefusesInvalidTOML(t *testing.T) {
	path := writeCase(t, "this is = = not toml")
	if status, _ := writeCodex(path, "treg", "u", "t", false); status != Failed {
		t.Fatal("invalid TOML was edited")
	}
}

func TestWrapMatchesTextwrap(t *testing.T) {
	got := Wrap(manualAgents[1].how, 76)
	want := []string{
		"dsh has no MCP config file to write - a server is a row in a PROFILE's",
		"config layer. Install the bundle instead, which carries that row: `dsh",
		"plugin --profile <name> add github:superdesigndev/treg`. The row stays",
		"disabled until TREG_TOKEN is in the environment dsh starts in, so export it",
		"and restart dsh.",
	}
	if strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Fatalf("got %q", got)
	}
	if got := Wrap(oauthManualHow, 76); len(got) != 3 || got[2] != "it." {
		t.Fatalf("got %q", got)
	}
}

// GitHub Copilot's user MCP file: `mcpServers` with a Streamable HTTP entry, the shape
// docs.github.com and VS Code document for $COPILOT_HOME/mcp-config.json (default ~/.copilot).
func copilotHomes(t *testing.T) (string, string) {
	t.Helper()
	h := t.TempDir()
	t.Setenv("HOME", h)
	t.Setenv("USERPROFILE", h)
	t.Setenv("PATH", t.TempDir())
	t.Setenv("XDG_CONFIG_HOME", filepath.Join(h, ".config"))
	t.Setenv("COPILOT_HOME", "")
	return h, filepath.Join(h, ".copilot")
}

func copilotResult(t *testing.T, out Installed) Result {
	t.Helper()
	for _, r := range out.Results {
		if r.Display == CopilotDisplay {
			return r
		}
	}
	t.Fatalf("no %s result in %+v", CopilotDisplay, out.Results)
	return Result{}
}

func TestCopilotGetsAnHTTPEntryBesideItsOtherServers(t *testing.T) {
	_, dir := copilotHomes(t)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	cfg := filepath.Join(dir, "mcp-config.json")
	if err := os.WriteFile(cfg, []byte(`{"mcpServers": {"gh": {"type": "local", "command": "x", "tools": ["*"]}}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	out := InstallMCP("https://treg.to", "tok", "treg", false)
	if r := copilotResult(t, out); r.Status != OK || r.Detail != cfg {
		t.Fatalf("result %+v", r)
	}
	want := "{\n  \"mcpServers\": {\n    \"gh\": {\n      \"type\": \"local\",\n      \"command\": \"x\",\n      \"tools\": [\n        \"*\"\n      ]\n    },\n" +
		"    \"treg\": {\n      \"type\": \"http\",\n      \"url\": \"https://treg.to/mcp/\",\n      \"headers\": {\n        \"Authorization\": \"Bearer tok\"\n      },\n" +
		"      \"tools\": [\n        \"*\"\n      ]\n    }\n  }\n}\n"
	if got := read(t, cfg); got != want {
		t.Fatalf("got:\n%s", got)
	}
	if runtime.GOOS != "windows" {
		if info, _ := os.Stat(cfg); info.Mode().Perm() != 0o600 {
			t.Errorf("mode %v, want 0600", info.Mode().Perm())
		}
	}
}

func TestCopilotOAuthEntryCarriesTheURLOnly(t *testing.T) {
	_, dir := copilotHomes(t)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	InstallMCP("https://treg.to/", "", "treg", true)
	got := read(t, filepath.Join(dir, "mcp-config.json"))
	if strings.Contains(got, "headers") || !strings.Contains(got, `"url": "https://treg.to/mcp/"`) {
		t.Fatalf("got:\n%s", got)
	}
}

func TestCopilotHomeReplacesTheDefaultDirectory(t *testing.T) {
	h, dir := copilotHomes(t)
	custom := filepath.Join(h, "elsewhere")
	if err := os.MkdirAll(custom, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("COPILOT_HOME", custom)
	out := InstallMCP("https://treg.to", "tok", "treg", false)
	if r := copilotResult(t, out); r.Status != OK || r.Detail != filepath.Join(custom, "mcp-config.json") {
		t.Fatalf("result %+v", r)
	}
	if Exists(dir) {
		t.Fatalf("%s was created although COPILOT_HOME is set", dir)
	}
}

func TestCopilotIsLeftAloneWhenNotInstalled(t *testing.T) {
	_, dir := copilotHomes(t)
	for _, r := range InstallMCP("https://treg.to", "tok", "treg", false).Results {
		if r.Display == CopilotDisplay {
			t.Fatalf("unexpected result %+v", r)
		}
	}
	if Exists(dir) {
		t.Fatalf("%s was created", dir)
	}
}
