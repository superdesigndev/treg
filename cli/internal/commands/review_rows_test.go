package commands

import (
	"testing"

	"github.com/superdesigndev/treg/cli/internal/ojson"
)

func TestParseReviewRows(t *testing.T) {
	items, ok := parseReviewRows(" 2/email:wrong, 5:missing ,title:stale,wrong,")
	if !ok || len(items) != 4 {
		t.Fatalf("got %v %v", items, ok)
	}
	got := string(ojson.Compact(ojson.Ordered("rows", items)))
	want := `{"rows":[{"verdict":"wrong","index":2,"field":"email"},{"verdict":"missing","index":5},{"verdict":"stale","field":"title"},{"verdict":"wrong"}]}`
	if got != want {
		t.Errorf("got %s\nwant %s", got, want)
	}
	for _, bad := range []string{"", ",", "2/email:great", "-1:wrong", "2/em ail:wrong"} {
		if _, ok := parseReviewRows(bad); ok {
			t.Errorf("%q should be refused", bad)
		}
	}
}
