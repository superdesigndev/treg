package ojson

import "testing"

func TestDecodeKeepsOrderAndNumberSpelling(t *testing.T) {
	v, err := Loads("\ufeff" + `{"b": 1.50, "a": [1, {"x": "é<&>"}], "b": 2e3, "n": null}`)
	if err != nil {
		t.Fatal(err)
	}
	o := v.(*Object)
	if got := o.Keys(); len(got) != 3 || got[0] != "b" || got[1] != "a" || got[2] != "n" {
		t.Errorf("keys %v: a name given twice keeps its first place", got)
	}
	if got := string(Compact(v)); got != `{"b":2e3,"a":[1,{"x":"é<&>"}],"n":null}` {
		t.Errorf("compact %s", got)
	}
}

func TestPrettyIsTwoSpaces(t *testing.T) {
	v := Ordered("a", []any{}, "b", NewObject(), "c", map[string]any{"z": 1, "y": []string(nil)})
	want := "{\n  \"a\": [],\n  \"b\": {},\n  \"c\": {\n    \"y\": [],\n    \"z\": 1\n  }\n}"
	if got := Pretty(v); got != want {
		t.Errorf("pretty\n%s\nwant\n%s", got, want)
	}
}

func TestDecodeRefusesWhatIsNotOneValue(t *testing.T) {
	for _, in := range []string{"", "  ", `{"a":1} x`, `{"a":}`, `[1,`} {
		if _, err := Loads(in); err == nil {
			t.Errorf("%q decoded", in)
		}
	}
}

func TestStringsThatAreNotUTF8AreReadAndWritten(t *testing.T) {
	v, err := Decode([]byte("{\"a\": \"x\xffy\"}"))
	if err != nil {
		t.Fatal(err)
	}
	if got := string(Compact(v)); got != `{"a":"x`+"�"+`y"}` {
		t.Errorf("compact %s", got)
	}
	if !Valid([]byte("{\"a\": 1, \"a\": \"\xff\"}")) {
		t.Error("a duplicate name or invalid UTF-8 made it invalid")
	}
}

func TestTextAndTruthy(t *testing.T) {
	for v, want := range map[any]string{nil: "", "s": "s", true: "true", Number("1.50"): "1.50"} {
		if got := Text(v); got != want {
			t.Errorf("Text(%v) = %q", v, got)
		}
	}
	if got := Text(Ordered("a", 1)); got != `{"a":1}` {
		t.Errorf("Text(object) = %q", got)
	}
	for _, v := range []any{nil, false, "", Number("0"), Number("0.0"), []any{}, NewObject()} {
		if Truthy(v) {
			t.Errorf("Truthy(%#v)", v)
		}
	}
}
