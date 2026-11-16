package output

import (
	"bytes"
	"net/http"
	"testing"

	"github.com/superdesigndev/treg/cli/internal/api"
)

func TestMarkHintPrintsTheRegistryText(t *testing.T) {
	for _, tc := range []struct{ text, want string }{
		{"mark rows of c_1 once you know", "treg: mark rows of c_1 once you know\n"},
		{"", ""},
	} {
		h := http.Header{}
		h.Set("X-Treg-Call-Id", "c_1")
		h.Set("X-Treg-Hint", "mark")
		if tc.text != "" {
			h.Set("X-Treg-Hint-Text", tc.text)
		}
		var errOut bytes.Buffer
		(&Printer{Err: &errOut}).hintLine(&api.Response{Status: 200, Header: h})
		if got := errOut.String(); got != tc.want {
			t.Errorf("text %q: got %q, want %q", tc.text, got, tc.want)
		}
	}
}
