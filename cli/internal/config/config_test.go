package config

import (
	"encoding/json/v2"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func sandbox(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.json")
	t.Setenv("TREG_CONFIG", path)
	if content != "" {
		os.WriteFile(path, []byte(content), 0o600)
	}
	return path
}

func TestReadsAndWritesThePythonLayout(t *testing.T) {
	path := sandbox(t, `{"base_url": "http://x", "token": "t", "email": "a@b.c", "active_org": "acme", "identity": true, "admin_token": null, "future": {"k": [1, 2]}}`)
	cfg := Load()
	if cfg.BaseURL != "http://x" || cfg.Token != "t" || cfg.Email != "a@b.c" || cfg.ActiveOrg != "acme" || !cfg.Identity {
		t.Fatalf("cfg = %+v", cfg)
	}
	cfg.Token = ""
	if err := cfg.Save(); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(path)
	want := "{\n  \"base_url\": \"http://x\",\n  \"token\": null,\n  \"email\": \"a@b.c\",\n  \"active_org\": \"acme\",\n" +
		"  \"identity\": true,\n  \"admin_token\": null,\n  \"future\": {\n    \"k\": [\n      1,\n      2\n    ]\n  }\n}"
	if string(raw) != want {
		t.Fatalf("wrote\n%s", raw)
	}
	if info, _ := os.Stat(path); runtime.GOOS != "windows" && info.Mode().Perm() != 0o600 {
		t.Fatalf("mode %v", info.Mode())
	}
}

func TestCorruptOrMissingConfigReadsAsFresh(t *testing.T) {
	for _, content := range []string{"", "{not json", "[]", `{"a":1} trailing`} {
		sandbox(t, content)
		if cfg := Load(); cfg.BaseURL != ProductionBaseURL || cfg.Token != "" {
			t.Fatalf("%q read as %+v", content, cfg)
		}
	}
}

func TestLegacyMultiOrgConfig(t *testing.T) {
	path := sandbox(t, `{"active_org": "b", "orgs": {"a": {"token": "ta"}, "b": {"token": "tb"}}, "email": "e"}`)
	cfg := Load()
	if cfg.Token != "tb" || cfg.Identity {
		t.Fatalf("cfg = %+v", cfg)
	}
	cfg.Save()
	var saved map[string]any
	raw, _ := os.ReadFile(path)
	json.Unmarshal(raw, &saved)
	if _, ok := saved["orgs"]; ok || saved["token"] != "tb" {
		t.Fatalf("saved %v", saved)
	}
}

func TestOneCommandOrgIsNotSaved(t *testing.T) {
	sandbox(t, `{"token": "mine", "active_org": "a"}`)
	cfg := Load()
	cfg.UseOnce("theirs", "b")
	cfg.Email = "changed"
	cfg.Save()
	again := Load()
	if again.Token != "mine" || again.ActiveOrg != "a" || again.Email != "changed" {
		t.Fatalf("saved %+v", again)
	}
}
