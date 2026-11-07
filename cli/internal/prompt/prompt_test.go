package prompt

import (
	"reflect"
	"testing"
)

func TestKeysSplitsOneRead(t *testing.T) {
	for in, want := range map[string][]string{
		" \r":         {" ", "\r"},
		"\x1b[Bj \r":  {"\x1b[B", "j", " ", "\r"},
		"\x1bOA":      {"\x1bOA"},
		"\x1b":        {"\x1b"},
		"a\x1b[A\x1b": {"a", "\x1b[A", "\x1b"},
	} {
		if got := keys([]byte(in)); !reflect.DeepEqual(got, want) {
			t.Errorf("keys(%q) = %q, want %q", in, got, want)
		}
	}
}
