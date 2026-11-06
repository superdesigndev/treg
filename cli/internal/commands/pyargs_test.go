package commands

import (
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"testing"

	"github.com/superdesigndev/treg/cli/internal/pyjson"
)

// The expected values in these tables are CPython 3.13's own answers.

func TestPyInt(t *testing.T) {
	for _, c := range []struct {
		in, want string
		ok       bool
	}{
		{"7", "7", true},
		{" 007 ", "7", true},
		{"+4", "4", true},
		{"-0", "0", true},
		{"1_000", "1000", true},
		{"1__0", "", false},
		{"_1", "", false},
		{"1_", "", false},
		{"", "", false},
		{" ", "", false},
		{"1.5", "", false},
		{"0x1f", "", false},
		{"-12", "-12", true},
		{"99999999999999999999", "99999999999999999999", true},
	} {
		got, ok := pyInt(c.in)
		if string(got) != c.want || ok != c.ok {
			t.Errorf("pyInt(%q) = %q, %v; want %q, %v", c.in, got, ok, c.want, c.ok)
		}
	}
}

func TestPyFloat(t *testing.T) {
	for _, c := range []struct {
		in, want string
		ok       bool
	}{
		{"1", "1.0", true},
		{" 2.5 ", "2.5", true},
		{"1e3", "1000.0", true},
		{"1_000.5", "1000.5", true},
		{"inf", "inf", true},
		{"-Infinity", "-inf", true},
		{"nan", "nan", true},
		{".5", "0.5", true},
		{"5.", "5.0", true},
		{"+1e-2", "0.01", true},
		{"1e400", "inf", true},
		{"0x10", "", false},
		{"1__0", "", false},
		{"abc", "", false},
		{"", "", false},
		{"1e5_0", "1e+50", true},
	} {
		f, ok := pyFloat(c.in)
		got := ""
		if ok {
			got = pyjson.Repr(f)
		}
		if got != c.want || ok != c.ok {
			t.Errorf("pyFloat(%q) = %q, %v; want %q, %v", c.in, got, ok, c.want, c.ok)
		}
	}
}

func TestPyPath(t *testing.T) {
	if runtime.GOOS == "windows" {
		for in, want := range map[string]string{"./skill/": "skill", `C:/a//b/`: `C:\a\b`, `C:\`: `C:\`, "a/./b": `a\b`} {
			if got := pyPath(in); got != want {
				t.Errorf("pyPath(%q) = %q, want %q", in, got, want)
			}
		}
		return
	}
	for _, c := range []struct{ in, want string }{
		{"", "."},
		{".", "."},
		{"./", "."},
		{"./skill/", "skill"},
		{"a//b/", "a/b"},
		{"/", "/"},
		{"//", "//"},
		{"///x", "/x"},
		{"//x/", "//x"},
		{"a/./b", "a/b"},
		{"a/../b", "a/../b"},
		{"~/x", "~/x"},
		{"x/.", "x"},
		{"../x", "../x"},
	} {
		if got := pyPath(c.in); got != c.want {
			t.Errorf("pyPath(%q) = %q, want %q", c.in, got, c.want)
		}
	}
	if got := pyJoin("./", ".secret"); got != ".secret" {
		t.Errorf("pyJoin = %q", got)
	}
	if got := pyJoin("/", "x"); got != "/x" {
		t.Errorf("pyJoin = %q", got)
	}
}

func TestPySlice(t *testing.T) {
	rows := []int{1, 2, 3, 4}
	for n, want := range map[int][]int{0: {}, 2: {1, 2}, 9: {1, 2, 3, 4}, -1: {1, 2, 3}, -9: {}} {
		if got := pySlice(rows, n); !reflect.DeepEqual(got, want) {
			t.Errorf("rows[:%d] = %v, want %v", n, got, want)
		}
	}
}

func TestOSErrorIsPythonsWording(t *testing.T) {
	_, err := os.ReadFile(filepath.Join(t.TempDir(), "missing.json"))
	if got := osError(err, "missing.json"); got != "[Errno 2] No such file or directory: 'missing.json'" {
		t.Errorf("osError = %q", got)
	}
}

func TestPySplitLines(t *testing.T) {
	for _, c := range []struct {
		in   string
		want []string
	}{
		{"a\nb", []string{"a", "b"}},
		{"a\r\nb\r\n", []string{"a", "b"}},
		{"a\rb", []string{"a", "b"}},
		{"a\vb\fc", []string{"a", "b", "c"}},
		{"a\x1cb c ", []string{"a", "b", "c"}},
		{"a\u0085b", []string{"a", "b"}},
		{"", nil},
		{"\n", []string{""}},
		{"x\n\ny", []string{"x", "", "y"}},
	} {
		if got := pySplitLines(c.in); !reflect.DeepEqual(got, c.want) {
			t.Errorf("splitlines(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}
