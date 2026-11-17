package commands

import "testing"

// The expectations are what the toon_format encoder the registry renders with writes.
func TestToonStringQuotesOnlyWhatAReaderWouldMisread(t *testing.T) {
	for in, want := range map[string]string{
		"1.0.0 at ~/bin/treg":   "1.0.0 at ~/bin/treg",
		"http://localhost:1234": `"http://localhost:1234"`,
		`C:\Users\x\treg.exe`:   `"C:\\Users\\x\\treg.exe"`,
		"":                      `""`,
		"-c":                    `"-c"`,
		"42":                    `"42"`,
		"1e5":                   `"1e5"`,
		"true":                  `"true"`,
		"a,b":                   `"a,b"`,
		" x":                    `" x"`,
		"x ":                    `"x "`,
		"tab\tx":                `"tab\tx"`,
		"#x":                    `"#x"`,
		"é ok":                  "é ok",
		"ctl\x01":               `"ctl\u0001"`,
	} {
		if got := toonString(in); got != want {
			t.Errorf("toonString(%q) = %s, want %s", in, got, want)
		}
	}
}

func TestCatalogArgsReadNewAsArgparseDid(t *testing.T) {
	a, _, err := parseCatalogArgs([]string{"search", "phone", "--new", "60", "--limit=5"})
	if err != nil || a.verb != "search" || len(a.rest) != 1 || *a.days != 60 || *a.limit != 5 {
		t.Fatalf("got %+v, %v", a, err)
	}
	a, _, err = parseCatalogArgs([]string{"search", "--new", "--sort", "newest"})
	if err != nil || *a.days != 30 || a.sort != "newest" || len(a.rest) != 0 {
		t.Fatalf("a bare --new is 30 days: got %+v, %v", a, err)
	}
	if _, _, err = parseCatalogArgs([]string{"search", "--new", "phone"}); err == nil {
		t.Fatal("--new phone is a mistake, not a search for phone")
	}
}
