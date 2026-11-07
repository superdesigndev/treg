package commands

import (
	"os"
	"path/filepath"
	"testing"
)

func TestNumberArguments(t *testing.T) {
	for in, want := range map[string]int{"7": 7, "007": 7, "+4": 4, "-12": -12} {
		if got, err := intArg("id", in); err != nil || got != want {
			t.Errorf("intArg(%q) = %d, %v; want %d", in, got, err, want)
		}
	}
	for _, in := range []string{"", " 7", "1_000", "1.5", "0x1f", "99999999999999999999"} {
		if _, err := intArg("id", in); err == nil {
			t.Errorf("intArg(%q) accepted", in)
		}
	}
	if _, err := countArg("--limit", "-1"); err == nil {
		t.Error("countArg accepted -1")
	}
	for in, want := range map[string]float64{"1": 1, "2.5": 2.5, "1e3": 1000, ".5": 0.5, "1_000.5": 1000.5} {
		if got, err := floatArg("amount", in); err != nil || got != want {
			t.Errorf("floatArg(%q) = %v, %v; want %v", in, got, err, want)
		}
	}
	for _, in := range []string{"", "abc", "inf", "nan", "1e400", " 1"} {
		if _, err := floatArg("amount", in); err == nil {
			t.Errorf("floatArg(%q) accepted", in)
		}
	}
}

func TestReadTextWantsUTF8AndUnixLineEndings(t *testing.T) {
	dir := t.TempDir()
	good, bad := filepath.Join(dir, "good"), filepath.Join(dir, "bad")
	os.WriteFile(good, []byte("a\r\nb\rc\n"), 0o600)
	os.WriteFile(bad, []byte("a\xffb"), 0o600)
	if got, err := readText(good); err != nil || got != "a\nb\nc\n" {
		t.Errorf("readText = %q, %v", got, err)
	}
	if _, err := readText(bad); err == nil || osError(err, "bad") != "bad: not UTF-8 text" {
		t.Errorf("readText(bad) = %v", err)
	}
}

func TestOSErrorNamesThePathAndTheReason(t *testing.T) {
	_, err := os.ReadFile(filepath.Join(t.TempDir(), "missing.json"))
	if got := osError(err, "missing.json"); got != "missing.json: no such file or directory" {
		t.Errorf("osError = %q", got)
	}
}
