package commands

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/superdesigndev/treg/cli/internal/config"
)

func useRefDir(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("TREG_CONFIG", filepath.Join(dir, ".treg", "config.json"))
	t.Setenv("TREG_REF", "")
	return filepath.Join(dir, ".treg", "ref")
}

func TestValidRefIsThePartnerCodeShape(t *testing.T) {
	for in, want := range map[string]string{
		"jane-doe": "jane-doe", " Jane-Doe \n": "jane-doe", "ab": "", "-jane": "", "jane doe": "",
		"a234567890123456789012345678901b":  "a234567890123456789012345678901b",
		"a2345678901234567890123456789012c": "",
	} {
		if got := validRef(in); got != want {
			t.Errorf("validRef(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestPendingRefPrefersTheSavedChoiceOverTheEnvironment(t *testing.T) {
	path := useRefDir(t)
	if pendingRef() != "" {
		t.Fatal("no code yet")
	}
	t.Setenv("TREG_REF", "env-code")
	if got := pendingRef(); got != "env-code" {
		t.Fatalf("with nothing saved, TREG_REF is used, got %q", got)
	}
	if err := rememberRef("saved-code"); err != nil {
		t.Fatal(err)
	}
	if raw, _ := os.ReadFile(path); string(raw) != "saved-code\n" {
		t.Fatalf("file holds %q, the code and a newline", raw)
	}
	if got := pendingRef(); got != "saved-code" {
		t.Fatalf("an explicit `login --ref` beats TREG_REF, got %q", got)
	}
	if err := os.WriteFile(path, []byte("Not A Code\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := pendingRef(); got != "env-code" {
		t.Fatalf("a malformed saved file falls back to TREG_REF, got %q", got)
	}
}

func TestSettleRefSpendsTheCodeOnlyForSomeoneWithATeam(t *testing.T) {
	path := useRefDir(t)
	_ = rememberRef("jane-doe")
	settleRef(&config.Config{})
	if _, err := os.Stat(path); err != nil {
		t.Fatal("without a team the code is kept for `treg org create`")
	}
	settleRef(&config.Config{ActiveOrg: "acme"})
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatal("a person who already has a team spent the code")
	}
}

func TestRefFlagRefusesAMalformedCode(t *testing.T) {
	if code, err := refFlag("", false); code != "" || err != nil {
		t.Fatal("no flag, no code")
	}
	if code, err := refFlag("Jane", true); code != "jane" || err != nil {
		t.Fatalf("got %q %v", code, err)
	}
	if _, err := refFlag("bad code", true); err == nil {
		t.Fatal("a malformed --ref must fail, not be dropped")
	}
}
