package skillscan

import (
	"os"
	"path/filepath"
	"slices"
	"testing"

	"github.com/superdesigndev/treg/cli/internal/ojson"
	"github.com/superdesigndev/treg/cli/internal/providers"
)

// testdata/scan_skills.json is the Python CLI's skills.scan_skills over this tree, with
// env_names={"OPENAI_API_KEY"}.
var tree = map[string]string{
	"multi/SKILL.md":                    "---\nname: multi\n---\nhttps://docs.multi.example/x and https://api.multi.example/v1 and http://other.example\n",
	"multi/.secrets/developer_token":    "dev\n",
	"multi/.secrets/token.json":         `{"token": "t", "refresh_token": "r"}`,
	"multi/.secrets/client_secret.json": `{"installed": {}}`,
	"multi/a-b/q.py":                    "x = 1",
	"multi/a.py":                        "BASE_URL = \"https://developer.example.com/api\"\n",
	"envy/SKILL.md":                     "---\nname: envy\n---\n",
	"envy/run.sh":                       "curl -H \"Authorization: Bearer ${OPENAI_API_KEY}\" -H \"x: $SECOND_TOKEN\" \"$PATH_X\"\n",
	"gsc/SKILL.md":                      "---\nname: gsc\n---\n",
	"gsc/.secret/token.json":            `{"access_token": "a"}`,
	"gsc/.secret/developer_token":       "d",
	"notes/SKILL.md":                    "---\nname: notes\n---\n",
	"notes/sub/x.txt":                   "a\r\nb",
	"notes/node_modules/y.js":           "no",
	"notes/bin.dat":                     "\xff\xfe\x00",
	"doppler/SKILL.md":                  "---\nname: doppler\n---\n",
	"bad/SKILL.md":                      "x",
	"bad/treg.json":                     `{"base_url": "https://x.example", "cli": {"bin": "a b"}}`,
	"contract/SKILL.md":                 "x",
	"contract/treg.json": `{"base_url": "https://c.example", "secrets": [{"file": ".secret/k", "name": "k"}, ` +
		`{"env": "MISSING_ENV", "name": "m"}], "bindings": [{"secret": "k", "name": "X-A"}, {"secret": "m", "name": "x-a"}], ` +
		`"health": {"path": "ping"}}`,
}

func build(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	for rel, text := range tree {
		p := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(text), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func orNil(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func view(d *Detection) any {
	secrets := []any{}
	for _, s := range d.Secrets {
		secrets = append(secrets, s)
	}
	gaps := []any{}
	for _, g := range d.Gaps {
		gaps = append(gaps, g)
	}
	bindings := d.Bindings
	if bindings == nil {
		bindings = []any{}
	}
	var cli any
	if d.CLI != nil {
		cli = d.CLI
	}
	return ojson.Ordered("name", d.Name, "kind", d.Kind, "base_url", orNil(d.BaseURL), "secrets", secrets,
		"bindings", bindings, "gaps", gaps, "note", orNil(d.Note), "cli", cli)
}

func TestScanSkillsMatchesPython(t *testing.T) {
	root := build(t)
	_, catalog := providers.Bundled()
	dets, err := ScanSkills(root, catalog, map[string]bool{"OPENAI_API_KEY": true})
	if err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile("testdata/scan_skills.json")
	if err != nil {
		t.Fatal(err)
	}
	wantV, _ := ojson.Decode(raw)
	want := wantV.([]any)
	if len(dets) != len(want) {
		t.Fatalf("%d skills, want %d", len(dets), len(want))
	}
	for i, d := range dets {
		w := want[i].(*ojson.Object)
		if d.Kind == "skip" {
			if d.Name != w.String("name") || w.String("kind") != "skip" {
				t.Errorf("skill %d: %s/%s, want a skipped %s", i, d.Name, d.Kind, w.String("name"))
			}
			continue
		}
		got := string(ojson.Compact(view(d)))
		if exp := string(ojson.Compact(w)); got != exp {
			t.Errorf("skill %s:\n got %s\nwant %s", d.Name, got, exp)
		}
	}
}

func TestCollectFiles(t *testing.T) {
	root := build(t)
	files := CollectFiles(filepath.Join(root, "notes"))
	if got := string(ojson.Compact(files)); got != `{"sub/x.txt":"a\nb"}` {
		t.Fatalf("got %s", got)
	}
}

func TestStemAndHeader(t *testing.T) {
	for name, want := range map[string]string{".env": ".env", "token.json": "token", "a.": "a.", "a.b.c": "a.b", "plain": "plain"} {
		if got := stem(name); got != want {
			t.Errorf("stem(%q) = %q, want %q", name, got, want)
		}
	}
	for s, want := range map[string]string{"developer_token": "developer-token", "__": "authorization", "A B": "a-b"} {
		if got := headerFromStem(s); got != want {
			t.Errorf("headerFromStem(%q) = %q, want %q", s, got, want)
		}
	}
}

func TestPathOrder(t *testing.T) {
	paths := []string{"a.py", "a-b/q.py", ".x.py", ".hid/y.py", "sub/z.py"}
	slices.SortStableFunc(paths, compareParts)
	want := []string{".hid/y.py", ".x.py", "a-b/q.py", "a.py", "sub/z.py"}
	for i := range want {
		if paths[i] != want[i] {
			t.Fatalf("got %v, want %v", paths, want)
		}
	}
}

func TestCLIProfileValidation(t *testing.T) {
	for text, want := range map[string]bool{
		`{"bin": "gh", "inject": [{"via": "env", "name": "GH_TOKEN"}]}`: true,
		`{"bin": "a b"}`:                                                              false,
		`{"enabled": "yes"}`:                                                          false,
		`{"auth_mechanism": "magic"}`:                                                 false,
		`{"inject": [{"via": "env", "name": "LD_PRELOAD"}]}`:                          false,
		`{"inject": [{"via": "argv", "argv": []}]}`:                                   false,
		`{"inject": [{"via": "env", "name": "X", "format": "no placeholder"}]}`:       false,
		`{"inject": [{"via": "env", "name": "X", "secret_id": 3}]}`:                   true,
		`{"inject": [{"via": "env", "name": "X", "secret_id": 3.5}]}`:                 false,
		`{"deny": ["(?=lookahead)x"], "errors": [{"pattern": "a", "verdict": "ok"}]}`: true,
		`{"deny": ["(unbalanced"]}`:                                                   false,
		`{"detect": {"config_paths": ["~/.x", ""]}}`:                                  false,
		`{"errors": [{"pattern": "a", "verdict": "maybe"}]}`:                          false,
	} {
		v, err := ojson.Decode([]byte(text))
		if err != nil {
			t.Fatal(err)
		}
		if got := validCLIProfile(v); got != want {
			t.Errorf("validCLIProfile(%s) = %v, want %v", text, got, want)
		}
	}
}

func TestBuildPayloadReadsSecretsAtUpload(t *testing.T) {
	root := build(t)
	_, catalog := providers.Bundled()
	dets, _ := ScanSkills(root, catalog, map[string]bool{"OPENAI_API_KEY": true})
	byName := map[string]*Detection{}
	for _, d := range dets {
		byName[d.Name] = d
	}
	p, err := BuildPayload(byName["multi"], nil)
	if err != nil {
		t.Fatal(err)
	}
	secrets, _ := p.Get("secrets")
	if got := string(ojson.Compact(secrets)); got !=
		`[{"local_name":"developer_token","value":"dev","kind":"env"},{"local_name":"token","value":"{\"token\": \"t\", \"refresh_token\": \"r\"}","kind":"oauth"}]` {
		t.Fatalf("secrets %s", got)
	}
	if _, err := BuildPayload(byName["envy"], map[string]string{}); err == nil || err.Error() != "missing env value for OPENAI_API_KEY" {
		t.Fatalf("err %v", err)
	}
	path, err := WriteContract(byName["envy"])
	if err != nil || path == "" {
		t.Fatalf("%q %v", path, err)
	}
	if again, _ := WriteContract(byName["envy"]); again != "" {
		t.Fatal("an existing treg.json was overwritten")
	}
}
