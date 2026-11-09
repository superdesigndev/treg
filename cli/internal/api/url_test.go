package api

import (
	"net/url"
	"testing"
)

// The expectations are what httpx 0.28 sends for the same input.
func TestRequestTargetMatchesHttpx(t *testing.T) {
	base, _ := url.Parse("http://h:1/pre")
	for rel, want := range map[string]string{
		"/call/t/a b/{x}/ü/%41/%zz/../q!*'()[]|^\\\"<>`": "/pre/call/t/a%20b/%7Bx%7D/%C3%BC/%41/q!*'()[]|^\\%22%3C%3E%60",
		"/call/https://api.x.com/v1/a":                   "/pre/call/https://api.x.com/v1/a",
		"/call/t/./x/../../y":                            "/pre/call/y",
	} {
		if got := requestTarget(base, rel); got != want {
			t.Errorf("requestTarget(%q) = %q, want %q", rel, got, want)
		}
	}
}

func TestEncodeQueryMatchesUrlencode(t *testing.T) {
	got := encodeQuery([][2]string{{"k", "a b"}, {"k", "c/d?&=+é~*!'()"}, {"e", ""}})
	if want := "k=a+b&k=c%2Fd%3F%26%3D%2B%C3%A9~%2A%21%27%28%29&e="; got != want {
		t.Fatalf("got %s", got)
	}
}
