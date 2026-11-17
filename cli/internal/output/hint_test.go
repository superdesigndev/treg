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

func TestJSONEnvelopeKeepsTheMarkHintText(t *testing.T) {
	h := http.Header{}
	h.Set("Content-Type", "application/json")
	h.Set("X-Treg-Call-Id", "c_1")
	h.Set("X-Treg-Hint", "mark")
	h.Set("X-Treg-Hint-Text", "mark rows of c_1 once you know")
	env := envelope(&api.Response{Status: 200, Header: h, Body: []byte(`{"people":[]}`)}, "application/json")
	if got := env.Treg.Value("hint"); got != "mark" {
		t.Errorf("hint: got %v", got)
	}
	if got := env.Treg.Value("hint_text"); got != "mark rows of c_1 once you know" {
		t.Errorf("hint_text: got %v", got)
	}
	h.Del("X-Treg-Hint-Text")
	h.Set("X-Treg-Hint", "review")
	if env := envelope(&api.Response{Status: 200, Header: h, Body: []byte(`{}`)}, "application/json"); env.Treg.Value("hint_text") != nil {
		t.Errorf("hint_text without a text header: %v", env.Treg.Value("hint_text"))
	}
}
