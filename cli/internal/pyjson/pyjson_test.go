package pyjson

import "testing"

func TestFloatReprMatchesPython(t *testing.T) {
	for f, want := range map[float64]string{
		0.1: "0.1", 1.0: "1.0", 1e16: "1e+16", 1e15: "1000000000000000.0",
		123456789012345678.0: "1.2345678901234568e+17", 0.0001: "0.0001", 0.00001: "1e-05",
		1.5e-7: "1.5e-07", 2.5: "2.5", 1e22: "1e+22", 3.14159: "3.14159", 100.0: "100.0",
		1.7976931348623157e308: "1.7976931348623157e+308", 5e-324: "5e-324",
	} {
		if got := FloatRepr(f); got != want {
			t.Errorf("FloatRepr(%v) = %s, want %s", f, got, want)
		}
	}
}

func TestDumpsMatchesPython(t *testing.T) {
	v, err := Decode([]byte(`{"a": [1, 2.50, {"b": []}], "é": "x\u007f\ud83d\ude00\t", "c": {}}`))
	if err != nil {
		t.Fatal(err)
	}
	want := "{\n  \"a\": [\n    1,\n    2.5,\n    {\n      \"b\": []\n    }\n  ],\n" +
		"  \"\\u00e9\": \"x\\u007f\\ud83d\\ude00\\t\",\n  \"c\": {}\n}"
	if got := Dumps(v, Options{Indent: 2, EnsureASCII: true}); got != want {
		t.Errorf("indented:\n%s\nwant:\n%s", got, want)
	}
	v, _ = Decode([]byte(`{"a": [1, 2.50, {"b": []}], "c": {}}`))
	if got := Dumps(v, Options{Indent: -1, EnsureASCII: true}); got != `{"a": [1, 2.5, {"b": []}], "c": {}}` {
		t.Errorf("compact: %s", got)
	}
}

func TestDuplicateKeysKeepFirstPositionLastValue(t *testing.T) {
	v, err := Decode([]byte(`{"a":1,"b":2,"a":3, "n": -0, "big": 123456789012345678901234567890, "e": 1E2}`))
	if err != nil {
		t.Fatal(err)
	}
	want := "{\n  \"a\": 3,\n  \"b\": 2,\n  \"n\": 0,\n  \"big\": 123456789012345678901234567890,\n  \"e\": 100.0\n}"
	if got := Dumps(v, Options{Indent: 2, EnsureASCII: true}); got != want {
		t.Errorf("got:\n%s\nwant:\n%s", got, want)
	}
}

func TestSortKeys(t *testing.T) {
	o := Ordered("b", "2", "a", "1")
	if got := Dumps(o, Options{Indent: 2, EnsureASCII: true, SortKeys: true}); got != "{\n  \"a\": \"1\",\n  \"b\": \"2\"\n}" {
		t.Errorf("got %s", got)
	}
}
