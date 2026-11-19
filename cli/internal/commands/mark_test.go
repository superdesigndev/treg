package commands

import (
	"testing"

	"github.com/superdesigndev/treg/cli/internal/ojson"
)

func TestParseMarkItem(t *testing.T) {
	cases := map[string]string{
		"wrong":                       `{"verdict":"wrong"}`,
		"3:stale":                     `{"verdict":"stale","index":3}`,
		"0/email:wrong_entity":        `{"verdict":"wrong_entity","index":0,"field":"email"}`,
		"phone:missing":               `{"verdict":"missing","field":"phone"}`,
		"/phone:missing":              `{"verdict":"missing","field":"phone"}`,
		"2/experience[0].title:stale": `{"verdict":"stale","index":2,"field":"experience[0].title"}`,
	}
	for raw, want := range cases {
		item, ok := parseMarkItem(raw)
		if !ok {
			t.Fatalf("%q: refused", raw)
		}
		if got := string(ojson.Compact(item)); got != want {
			t.Errorf("%q: got %s, want %s", raw, got, want)
		}
	}
	for _, raw := range []string{"", "great", "3:", ":wrong", "-1:wrong", "x/y:wrong", "a b:wrong", "3:excellent"} {
		if _, ok := parseMarkItem(raw); ok {
			t.Errorf("%q: accepted", raw)
		}
	}
}
