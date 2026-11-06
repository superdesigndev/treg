package pyjson

import (
	"encoding/json"
	"math"
	"testing"
)

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

// The expected values are CPython 3.13's own output for each input.

type vector struct {
	in, pretty, body, line, err string
}

var vectors = []vector{
	{"{\"a\": 1, \"b\": [1.5, 2, -0, -0.0, 1e2, 1E400, 1e-400, 0.1, 1e16, 1e15, 123456789012345678901234567890]}", "{\u000a  \"a\": 1,\u000a  \"b\": [\u000a    1.5,\u000a    2,\u000a    0,\u000a    -0.0,\u000a    100.0,\u000a    Infinity,\u000a    0.0,\u000a    0.1,\u000a    1e+16,\u000a    1000000000000000.0,\u000a    123456789012345678901234567890\u000a  ]\u000a}", "{\"a\":1,\"b\":[1.5,2,0,-0.0,100.0,Infinity,0.0,0.1,1e+16,1000000000000000.0,123456789012345678901234567890]}", "{\"a\": 1, \"b\": [1.5, 2, 0, -0.0, 100.0, Infinity, 0.0, 0.1, 1e+16, 1000000000000000.0, 123456789012345678901234567890]}", ""},
	{"{\"s\": \"caf\\u00e9 \\ud83d\\ude00 <&> \\u2028 \\u007f \\u0001 \\/ \\b\\f\\n\\r\\t\"}", "{\u000a  \"s\": \"caf\\u00e9 \\ud83d\\ude00 <&> \\u2028 \\u007f \\u0001 / \\b\\f\\n\\r\\t\"\u000a}", "{\"s\":\"caf\u00e9 \U0001f600 <&> \u2028 \u007f \\u0001 / \\b\\f\\n\\r\\t\"}", "{\"s\": \"caf\\u00e9 \\ud83d\\ude00 <&> \\u2028 \\u007f \\u0001 / \\b\\f\\n\\r\\t\"}", ""},
	{"{\"dup\": 1, \"x\": 2, \"dup\": 3}", "{\u000a  \"dup\": 3,\u000a  \"x\": 2\u000a}", "{\"dup\":3,\"x\":2}", "{\"dup\": 3, \"x\": 2}", ""},
	{"[]", "[]", "[]", "[]", ""},
	{"{}", "{}", "{}", "{}", ""},
	{"[[], {}, [{}]]", "[\u000a  [],\u000a  {},\u000a  [\u000a    {}\u000a  ]\u000a]", "[[],{},[{}]]", "[[], {}, [{}]]", ""},
	{" \u0009\u000a 7 \u000d\u000a", "7", "7", "7", ""},
	{"[NaN, Infinity, -Infinity]", "[\u000a  NaN,\u000a  Infinity,\u000a  -Infinity\u000a]", "[NaN,Infinity,-Infinity]", "[NaN, Infinity, -Infinity]", ""},
	{"true", "true", "true", "true", ""},
	{"false", "false", "false", "false", ""},
	{"null", "null", "null", "null", ""},
	{"-1.5e-07", "-1.5e-07", "-1.5e-07", "-1.5e-07", ""},
	{"1.5e+300", "1.5e+300", "1.5e+300", "1.5e+300", ""},
	{"0.0001", "0.0001", "0.0001", "0.0001", ""},
	{"0.00001", "1e-05", "1e-05", "1e-05", ""},
	{"1000000.0", "1000000.0", "1000000.0", "1000000.0", ""},
	{"[5e-324, 1.7976931348623157e308]", "[\u000a  5e-324,\u000a  1.7976931348623157e+308\u000a]", "[5e-324,1.7976931348623157e+308]", "[5e-324, 1.7976931348623157e+308]", ""},
	{"\"\u00fcn\u00efcode \u2713 \u4e2d\u6587 \U0001f600\"", "\"\\u00fcn\\u00efcode \\u2713 \\u4e2d\\u6587 \\ud83d\\ude00\"", "\"\u00fcn\u00efcode \u2713 \u4e2d\u6587 \U0001f600\"", "\"\\u00fcn\\u00efcode \\u2713 \\u4e2d\\u6587 \\ud83d\\ude00\"", ""},
	{"", "", "", "", "Expecting value: line 1 column 1 (char 0)"},
	{" ", "", "", "", "Expecting value: line 1 column 2 (char 1)"},
	{"{", "", "", "", "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"},
	{"[", "", "", "", "Expecting value: line 1 column 2 (char 1)"},
	{"{\"a\"", "", "", "", "Expecting ':' delimiter: line 1 column 5 (char 4)"},
	{"{\"a\":", "", "", "", "Expecting value: line 1 column 6 (char 5)"},
	{"{\"a\":1", "", "", "", "Expecting ',' delimiter: line 1 column 7 (char 6)"},
	{"{\"a\":1,", "", "", "", "Expecting property name enclosed in double quotes: line 1 column 8 (char 7)"},
	{"{\"a\":1,}", "", "", "", "Illegal trailing comma before end of object: line 1 column 7 (char 6)"},
	{"[1,]", "", "", "", "Illegal trailing comma before end of array: line 1 column 3 (char 2)"},
	{"[1 2]", "", "", "", "Expecting ',' delimiter: line 1 column 4 (char 3)"},
	{"{\"a\" 1}", "", "", "", "Expecting ':' delimiter: line 1 column 6 (char 5)"},
	{"{1:2}", "", "", "", "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"},
	{"\"abc", "", "", "", "Unterminated string starting at: line 1 column 1 (char 0)"},
	{"\"a\\", "", "", "", "Unterminated string starting at: line 1 column 1 (char 0)"},
	{"\"a\\x\"", "", "", "", "Invalid \\escape: line 1 column 3 (char 2)"},
	{"\"\\u12\"", "", "", "", "Invalid \\uXXXX escape: line 1 column 3 (char 2)"},
	{"\"\\u12zz\"", "", "", "", "Invalid \\uXXXX escape: line 1 column 3 (char 2)"},
	{"\"a\u0009b\"", "", "", "", "Invalid control character at: line 1 column 3 (char 2)"},
	{"01", "", "", "", "Extra data: line 1 column 2 (char 1)"},
	{"1.", "", "", "", "Extra data: line 1 column 2 (char 1)"},
	{"-", "", "", "", "Expecting value: line 1 column 1 (char 0)"},
	{"- 1", "", "", "", "Expecting value: line 1 column 1 (char 0)"},
	{"[1]x", "", "", "", "Extra data: line 1 column 4 (char 3)"},
	{"nul", "", "", "", "Expecting value: line 1 column 1 (char 0)"},
	{"tru", "", "", "", "Expecting value: line 1 column 1 (char 0)"},
	{"1e", "", "", "", "Extra data: line 1 column 2 (char 1)"},
	{"1e+", "", "", "", "Extra data: line 1 column 2 (char 1)"},
	{"{\"a\":1}\u000a{\"b\":2}", "", "", "", "Extra data: line 2 column 1 (char 8)"},
	{"\ufeff{}", "", "", "", "Unexpected UTF-8 BOM (decode using utf-8-sig): line 1 column 1 (char 0)"},
	{"[\u000a1,\u000a2,\u000a]", "", "", "", "Illegal trailing comma before end of array: line 3 column 2 (char 6)"},
	{"{\u000a\"a\": x}", "", "", "", "Expecting value: line 2 column 6 (char 7)"},
	{"[-Infinity, -Inf]", "", "", "", "Expecting value: line 1 column 13 (char 12)"},
	{"1.5.2", "", "", "", "Extra data: line 1 column 4 (char 3)"},
	{"\"\u00fc\\q\"", "", "", "", "Invalid \\escape: line 1 column 3 (char 2)"},
	{"{\"\u00fc\": [1, \"\\u12", "", "", "", "Invalid \\uXXXX escape: line 1 column 13 (char 12)"},
	{"{\"k\": 1 , }", "", "", "", "Illegal trailing comma before end of object: line 1 column 9 (char 8)"},
}

func TestLoadsDumpsMatchPython(t *testing.T) {
	for _, v := range vectors {
		got, err := Loads(v.in)
		if v.err != "" {
			if err == nil || err.Error() != v.err {
				t.Errorf("Loads(%q) error = %v, want %q", v.in, err, v.err)
			}
			continue
		}
		if err != nil {
			t.Errorf("Loads(%q): %v", v.in, err)
			continue
		}
		for _, c := range []struct {
			opt  Options
			want string
		}{{Pretty, v.pretty}, {Body, v.body}, {Line, v.line}} {
			if out := Dumps(got, c.opt); out != c.want {
				t.Errorf("Dumps(Loads(%q), %+v) = %q, want %q", v.in, c.opt, out, c.want)
			}
		}
	}
}

func TestFloatReprVectors(t *testing.T) {
	for _, c := range []struct {
		f    float64
		want string
	}{
		{0.1, "0.1"},
		{100.0, "100.0"},
		{1e+16, "1e+16"},
		{1000000000000000.0, "1000000000000000.0"},
		{1e-05, "1e-05"},
		{0.0001, "0.0001"},
		{math.Copysign(0, -1), "-0.0"},
		{5.0, "5.0"},
		{2.5e-10, "2.5e-10"},
		{123456.789, "123456.789"},
		{1.7976931348623157e+308, "1.7976931348623157e+308"},
		{5e-324, "5e-324"},
		{1.2345678901234568e+16, "1.2345678901234568e+16"},
		{0.5, "0.5"},
	} {
		if got := FloatRepr(c.f); got != c.want {
			t.Errorf("FloatRepr(%v) = %q, want %q", c.f, got, c.want)
		}
	}
}

func TestReprAndText(t *testing.T) {
	v, _ := Loads(`{"a": [1, 2.50, null, true, "it's", 1e400], "b": "x\ny"}`)
	if got, want := Repr(v), `{'a': [1, 2.5, None, True, "it's", inf], 'b': 'x\ny'}`; got != want {
		t.Errorf("Repr = %s, want %s", got, want)
	}
	if got := Text(nil) + " " + Text("s") + " " + Text(json.Number("-0")); got != "None s 0" {
		t.Errorf("Text = %q", got)
	}
}
