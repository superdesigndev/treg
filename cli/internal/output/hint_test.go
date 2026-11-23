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

func TestEveryInvitationPrintsTheRegistryTextWhenSent(t *testing.T) {
	for _, kind := range []string{"review", "feedback"} {
		h := http.Header{}
		h.Set("X-Treg-Call-Id", "c_1")
		h.Set("X-Treg-Hint", kind)
		h.Set("X-Treg-Hint-Text", "server words for c_1")
		var errOut bytes.Buffer
		(&Printer{Err: &errOut}).hintLine(&api.Response{Status: 200, Header: h})
		if got := errOut.String(); got != "treg: server words for c_1\n" {
			t.Errorf("%s: got %q", kind, got)
		}
		// Without the text header the built-in line (the Python CLI's) is printed.
		h.Del("X-Treg-Hint-Text")
		errOut.Reset()
		(&Printer{Err: &errOut}).hintLine(&api.Response{Status: 200, Header: h})
		if got := errOut.String(); got == "" || got == "treg: server words for c_1\n" {
			t.Errorf("%s fallback: got %q", kind, got)
		}
	}
}

func compareHeaders() http.Header {
	h := http.Header{}
	h.Set("X-Treg-Call-Id", "c_1")
	h.Set("X-Treg-Hint", "compare")
	h.Set("X-Treg-Hint-Text", "treg compare shows both")
	return h
}

func waiting(status int, body string) func() *api.Response {
	return func() *api.Response { return &api.Response{Status: status, Body: []byte(body)} }
}

func TestACompareInvitationPrintsThePairAndTheRegistryWords(t *testing.T) {
	var errOut bytes.Buffer
	p := &Printer{Err: &errOut, Comparison: waiting(200,
		`{"comparison_id":7,"answer_1":"a","answer_2":"b","how_to_vote":"judge: treg compare 7 1|2|tie|both_bad"}`)}
	p.hintLine(&api.Response{Status: 200, Header: compareHeaders()})
	want := "treg: comparison waiting: {\"answer_1\":\"a\",\"answer_2\":\"b\",\"comparison_id\":7}\n" +
		"treg: judge: treg compare 7 1|2|tie|both_bad\n"
	if got := errOut.String(); got != want {
		t.Errorf("got %q, want %q", got, want)
	}
}

func TestACompareInvitationFallsBackToTheWordsWhenNoPairIsRead(t *testing.T) {
	for _, fetch := range []func() *api.Response{nil, waiting(204, ""), waiting(500, "{}"),
		func() *api.Response { return nil }} {
		var errOut bytes.Buffer
		(&Printer{Err: &errOut, Comparison: fetch}).hintLine(&api.Response{Status: 200, Header: compareHeaders()})
		if got := errOut.String(); got != "treg: treg compare shows both\n" {
			t.Errorf("got %q", got)
		}
	}
}

func TestTheJSONEnvelopeCarriesTheWaitingPair(t *testing.T) {
	var out bytes.Buffer
	h := compareHeaders()
	h.Set("Content-Type", "application/json")
	p := &Printer{Out: &out, Err: &bytes.Buffer{}, JSON: true,
		Comparison: waiting(200, `{"comparison_id":7,"answer_1":"a","answer_2":"b","how_to_vote":"vote 7"}`)}
	if err := p.ShowCall(&api.Response{Status: 200, Header: h, Body: []byte(`{"ok":true}`), Path: "/call/x"}); err != nil {
		t.Fatal(err)
	}
	got := out.String()
	for _, part := range []string{`"compare":{"answer_1":"a","answer_2":"b","comparison_id":7}`, `"hint_text":"vote 7"`} {
		if !bytes.Contains([]byte(got), []byte(part)) {
			t.Errorf("envelope %s lacks %s", got, part)
		}
	}
}

func TestAVoteKeepsTheOneOffOrgTheCallUsed(t *testing.T) {
	var errOut bytes.Buffer
	p := &Printer{Err: &errOut, OrgOverride: "team-b",
		Comparison: waiting(200, `{"comparison_id":7,"how_to_vote":"send treg compare 7 1|2|tie|both_bad"}`)}
	p.hintLine(&api.Response{Status: 200, Header: compareHeaders()})
	if !bytes.Contains(errOut.Bytes(), []byte("send treg --org team-b compare 7 1|2|tie|both_bad")) {
		t.Errorf("got %q", errOut.String())
	}
}

func TestAFailedReadStillKeepsTheOneOffOrg(t *testing.T) {
	var errOut bytes.Buffer
	(&Printer{Err: &errOut, OrgOverride: "team-b", Comparison: waiting(503, "{}")}).
		hintLine(&api.Response{Status: 200, Header: compareHeaders()})
	if got := errOut.String(); got != "treg: treg --org team-b compare shows both\n" {
		t.Errorf("got %q", got)
	}
	var out bytes.Buffer
	h := compareHeaders()
	h.Set("Content-Type", "application/json")
	p := &Printer{Out: &out, Err: &bytes.Buffer{}, JSON: true, OrgOverride: "team-b", Comparison: waiting(503, "{}")}
	if err := p.ShowCall(&api.Response{Status: 200, Header: h, Body: []byte(`{}`), Path: "/call/x"}); err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(out.Bytes(), []byte(`"hint_text":"treg --org team-b compare shows both"`)) {
		t.Errorf("envelope %s", out.String())
	}
}
