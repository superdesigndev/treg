package commands

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/spf13/pflag"

	"github.com/superdesigndev/treg/cli/internal/output"
	"github.com/superdesigndev/treg/cli/internal/pyjson"
)

func TestUSDMatchesPython(t *testing.T) {
	for _, c := range []struct {
		in   any
		want string
	}{
		{json.Number("0"), "$0.00"},
		{json.Number("1"), "$0.000001"},
		{json.Number("600"), "$0.0006"},
		{json.Number("-600"), "-$0.0006"},
		{json.Number("500000"), "$0.50"},
		{json.Number("999999"), "$0.999999"},
		{json.Number("1000000"), "$1.00"},
		{json.Number("1005000"), "$1.01"},
		{json.Number("1234567890"), "$1,234.57"},
		{json.Number("-1234567890"), "-$1,234.57"},
		{json.Number("1000000000000000"), "$1,000,000,000.00"},
		{int64(7_000_000), "$7.00"},
		{nil, "-"},
		{"12", "-"},
		{json.Number("1500000.5"), "-"},
	} {
		if got := usd(c.in); got != c.want {
			t.Errorf("usd(%#v) = %q, want %q", c.in, got, c.want)
		}
	}
}

func writeFiles(t *testing.T, root string, files map[string]string) {
	t.Helper()
	for rel, text := range files {
		path := filepath.Join(root, rel)
		if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(text), 0o600); err != nil {
			t.Fatal(err)
		}
	}
}

func TestFindSecretFile(t *testing.T) {
	dir := t.TempDir()
	writeFiles(t, dir, map[string]string{
		".secret/token":        "tok",
		".secret/client.json":  `{"client_id": "c", "client_secret": "s"}`,
		".secrets/oauth.json":  `{"refresh_token": "r"}`,
		".secrets/broken.json": `{`,
	})
	for kind, want := range map[string]string{
		"env":         filepath.Join(dir, ".secret/token"),
		"cli_auth":    filepath.Join(dir, ".secret/token"),
		"oauth":       filepath.Join(dir, ".secrets/oauth.json"),
		"secret_file": filepath.Join(dir, ".secrets/oauth.json"),
	} {
		if got, err := findSecretFile(dir, kind); err != nil || got != want {
			t.Errorf("findSecretFile(%s) = %q, %v; want %q", kind, got, err, want)
		}
	}
	writeFiles(t, dir, map[string]string{".secret/second": "x"})
	_, err := findSecretFile(dir, "env")
	if want := `ambiguous "env" secret in ` + dir + ": second, token - use --file"; err == nil || err.Error() != want {
		t.Errorf("ambiguous: %v, want %s", err, want)
	}
	empty := t.TempDir()
	if _, err := findSecretFile(empty, "env"); err == nil || err.Error() != "no .secret/.secrets files under "+empty {
		t.Errorf("none: %v", err)
	}
}

func TestContractSync(t *testing.T) {
	dir := t.TempDir()
	// The entry's file is the relative path in this OS's spelling.
	rel := strings.ReplaceAll(filepath.FromSlash(".secret/token.json"), `\`, `\\`)
	writeFiles(t, dir, map[string]string{
		".secret/token.json": `{"token": "t"}`,
		"treg.json":          `{"name": "s", "secrets": [{"file": "` + rel + `", "name": "old", "kind": "env", "n": 1.0}], "ü": "é"}`,
	})
	contract := filepath.Join(dir, "treg.json")
	if err := updateContract(contract, dir, filepath.Join(dir, ".secret/token.json"), "NEW", "secret_file"); err != nil {
		t.Fatal(err)
	}
	got, _ := os.ReadFile(contract)
	want := "{\n  \"name\": \"s\",\n  \"secrets\": [\n    {\n      \"file\": \"" + rel + "\",\n" +
		"      \"name\": \"NEW\",\n      \"kind\": \"secret_file\",\n      \"n\": 1.0\n    }\n  ],\n  \"\\u00fc\": \"\\u00e9\"\n}"
	if string(got) != want {
		t.Errorf("treg.json =\n%s\nwant\n%s", got, want)
	}
	writeFiles(t, dir, map[string]string{"treg.json": `{"name": "s"}`})
	if err := updateContract(contract, dir, filepath.Join(dir, ".secret/token.json"), "A", "oauth"); err != nil {
		t.Fatal(err)
	}
	got, _ = os.ReadFile(contract)
	if !strings.Contains(string(got), `"secrets": [`+"\n    {\n      \"file\": \""+rel+"\",\n      \"name\": \"A\",\n      \"kind\": \"oauth\"") {
		t.Errorf("appended entry missing:\n%s", got)
	}
	writeFiles(t, dir, map[string]string{"treg.json": "{\n\"a\": }"})
	err := updateContract(contract, dir, filepath.Join(dir, ".secret/token.json"), "A", "oauth")
	if err == nil {
		t.Errorf("malformed: %v", err)
	}
}

func TestParseBinds(t *testing.T) {
	got, err := parseBinds([]string{"secret=1", " secret_id = 02 , name=developer-token,format={secret}"})
	if err != nil {
		t.Fatal(err)
	}
	if body := pyjson.Dumps(got, pyjson.Body); body != `[{"injector":"env","location":"header","name":"Authorization",`+
		`"format":"Bearer {secret}","secret_field":"access_token","secret_id":1},{"injector":"env","location":"header",`+
		`"name":"developer-token","format":"{secret}","secret_field":"access_token","secret_id":2}]` {
		t.Errorf("bindings = %s", body)
	}
	for spec, want := range map[string]string{
		"name=X":        "each --bind needs secret=<id>",
		"secret=abc":    `--bind secret= must be an integer id, got "abc"`,
		"secret=1,x=2":  `unknown --bind key "x"`,
		"secret=1,":     `unknown --bind key ""`,
		"secret=1,auth": `unknown --bind key "auth"`,
	} {
		_, err := parseBinds([]string{spec})
		var exit *output.Exit
		if !errors.As(err, &exit) || exit.Message != want {
			t.Errorf("parseBinds(%q) = %v, want %q", spec, err, want)
		}
	}
}

func TestParseScopes(t *testing.T) {
	for _, c := range []struct {
		argv   []string
		scopes []string
		args   []string
	}{
		{[]string{"gsc", "--scopes", "a", "b", "--provider", "p"}, []string{"a", "b"}, []string{"gsc"}},
		{[]string{"--scopes", "a", "gsc"}, []string{"a", "gsc"}, nil},
		{[]string{"--scopes=a", "gsc"}, []string{"a"}, []string{"gsc"}},
		{[]string{"--scopes", "-1", "-x y"}, []string{"-1", "-x y"}, nil},
		{[]string{"gsc", "--", "--scopes"}, nil, []string{"gsc", "--scopes"}},
	} {
		fs := pflag.NewFlagSet("connect", pflag.ContinueOnError)
		var scopes []string
		fs.StringArrayVar(&scopes, "scopes", nil, "")
		fs.String("provider", "", "")
		args, err := parseScopes(fs, c.argv)
		if len(args) == 0 {
			args = nil
		}
		if err != nil || !reflect.DeepEqual(scopes, c.scopes) || !reflect.DeepEqual(args, c.args) {
			t.Errorf("parseScopes(%q) = %q, %q, %v; want %q, %q", c.argv, scopes, args, err, c.scopes, c.args)
		}
	}
	fs := pflag.NewFlagSet("connect", pflag.ContinueOnError)
	fs.StringArray("scopes", nil, "")
	fs.Bool("key-stdin", false, "")
	if _, err := parseScopes(fs, []string{"--scopes", "--key-stdin"}); err == nil {
		t.Error("a bare --scopes with no value must be a usage error")
	}
}
