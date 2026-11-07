package commands

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/providers"
)

// group is a command that only holds subcommands. Run bare, or with a word that is none of them,
// it is a usage error, as an argparse group with required subcommands is.
func group(use, short string, examples ...string) *cobra.Command {
	return &cobra.Command{
		Use:     use + " <subcommand>",
		Short:   short,
		Long:    short,
		Example: examplesText(examples...),
		Args:    cobra.ArbitraryArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			if len(args) == 0 {
				return missingSubcommand(cmd, args)
			}
			return invalidSubcommand(cmd, args[0])
		},
	}
}

func invalidSubcommand(cmd *cobra.Command, word string) error {
	var names []string
	for _, c := range cmd.Commands() {
		if !c.Hidden && c.Name() != "help" {
			names = append(names, c.Name())
		}
	}
	return fmt.Errorf("argument <subcommand>: invalid choice: %s (choose from %s)", quote(word), strings.Join(names, ", "))
}

func examplesText(lines ...string) string {
	if len(lines) == 0 {
		return ""
	}
	return "  " + strings.Join(lines, "\n  ")
}

// leaf is one runnable command with its description and copy-paste examples.
func leaf(use, short string, examples ...string) *cobra.Command {
	return &cobra.Command{Use: use, Short: short, Long: short, Example: examplesText(examples...)}
}

// choice is argparse's `choices=`: a value outside them is a usage error.
func choice(flag, value string, options ...string) error {
	for _, o := range options {
		if value == o {
			return nil
		}
	}
	quoted := make([]string, len(options))
	for i, o := range options {
		quoted[i] = quote(o)
	}
	return fmt.Errorf("argument %s: invalid choice: %s (choose from %s)", flag, quote(value), strings.Join(quoted, ", "))
}

// intArg is argparse's `type=int`: Python's int() of the text, else a usage error.
func intArg(name, s string) (json.Number, error) {
	n, ok := pyInt(s)
	if !ok {
		return "", fmt.Errorf("argument %s: invalid int value: %s", name, quote(s))
	}
	return n, nil
}

// pyInt is Python's int(str) in base 10: surrounding whitespace, a sign, leading zeros and single
// underscores between digits are allowed. It returns the value's decimal spelling.
func pyInt(s string) (json.Number, bool) {
	s = providers.Strip(s)
	neg := false
	if s != "" && (s[0] == '+' || s[0] == '-') {
		neg = s[0] == '-'
		s = s[1:]
	}
	digits, ok := withoutUnderscores(s)
	if !ok || digits == "" {
		return "", false
	}
	for _, c := range digits {
		if c < '0' || c > '9' {
			return "", false
		}
	}
	digits = strings.TrimLeft(digits, "0")
	if digits == "" {
		return "0", true
	}
	if neg {
		digits = "-" + digits
	}
	return json.Number(digits), true
}

// pyIntValue is an int's value, clamped to the platform's int range.
func pyIntValue(n json.Number) (int, bool) {
	v, err := strconv.ParseInt(string(n), 10, 0)
	if err != nil {
		var num *strconv.NumError
		if !errors.As(err, &num) || num.Err != strconv.ErrRange {
			return 0, false
		}
	}
	return int(v), true
}

// withoutUnderscores drops PEP 515 underscores, each of which must sit between two digits.
func withoutUnderscores(s string) (string, bool) {
	if !strings.Contains(s, "_") {
		return s, true
	}
	for i := 0; i < len(s); i++ {
		if s[i] == '_' && (i == 0 || i == len(s)-1 || !isDigitByte(s[i-1]) || !isDigitByte(s[i+1])) {
			return "", false
		}
	}
	return strings.ReplaceAll(s, "_", ""), true
}

func isDigitByte(c byte) bool { return c >= '0' && c <= '9' }

// floatArg is argparse's `type=float`.
func floatArg(name, s string) (float64, error) {
	f, ok := pyFloat(s)
	if !ok {
		return 0, fmt.Errorf("argument %s: invalid float value: %s", name, quote(s))
	}
	return f, nil
}

// pyFloat is Python's float(str): decimal or scientific notation, inf and nan, PEP 515 underscores.
func pyFloat(s string) (float64, bool) {
	s = providers.Strip(s)
	body := strings.TrimLeft(s, "+-")
	if len(s)-len(body) > 1 || strings.ContainsAny(body, "xXpP") {
		return 0, false
	}
	plain, ok := withoutUnderscores(s)
	if !ok {
		return 0, false
	}
	f, err := strconv.ParseFloat(plain, 64)
	if err != nil {
		var num *strconv.NumError
		if errors.As(err, &num) && num.Err == strconv.ErrRange {
			return f, true
		}
		return 0, false
	}
	return f, true
}

// pyPath is str(Path(p)): separators repeated or trailing and "." segments dropped, ".." kept, and
// on Windows every "/" written as "\\", as WindowsPath prints it.
func pyPath(p string) string {
	if p == "" {
		return "."
	}
	sep := string(filepath.Separator)
	if sep != "/" {
		p = strings.ReplaceAll(p, "/", sep)
	}
	lead := filepath.VolumeName(p)
	rest := p[len(lead):]
	switch {
	case lead == "" && sep == "/" && strings.HasPrefix(rest, "//") && !strings.HasPrefix(rest, "///"):
		lead = "//"
	case strings.HasPrefix(rest, sep):
		lead += sep
	}
	var parts []string
	for _, part := range strings.Split(rest, sep) {
		if part != "" && part != "." {
			parts = append(parts, part)
		}
	}
	out := lead + strings.Join(parts, sep)
	if out == "" {
		return "."
	}
	return out
}

// pyJoin is str(Path(base) / name).
func pyJoin(base, name string) string {
	base = pyPath(base)
	sep := string(filepath.Separator)
	switch {
	case base == ".":
		return name
	case strings.HasSuffix(base, sep):
		return base + name
	}
	return base + sep + name
}

// pyName is Path(p).name of a path pyPath has normalized.
func pyName(p string) string {
	return p[strings.LastIndex(p, string(filepath.Separator))+1:]
}

func exists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

// osError says why path could not be used: the path and the system's reason, without the
// operation Go's error also names.
func osError(err error, path string) string {
	var pe *fs.PathError
	if errors.As(err, &pe) {
		return path + ": " + pe.Err.Error()
	}
	return err.Error()
}

// universalNewlines is Python's text-mode read: "\r\n" and "\r" become "\n".
func universalNewlines(s string) string {
	return strings.ReplaceAll(strings.ReplaceAll(s, "\r\n", "\n"), "\r", "\n")
}

// stdinText is sys.stdin.read(): Python translates newlines on stdin only on Windows.
func stdinText(raw []byte) string {
	if runtime.GOOS == "windows" {
		return universalNewlines(string(raw))
	}
	return string(raw)
}

// readText is Path.read_text(encoding="utf-8").
func readText(path string) (string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	for i := 0; i < len(raw); {
		r, size := utf8.DecodeRune(raw[i:])
		if r == utf8.RuneError && size == 1 {
			return "", fmt.Errorf("'utf-8' codec can't decode byte 0x%02x in position %d", raw[i], i)
		}
		i += size
	}
	return universalNewlines(string(raw)), nil
}

// pySlice is Python's rows[:n], a negative n counting from the end.
func pySlice[T any](rows []T, n int) []T {
	if n < 0 {
		n += len(rows)
		if n < 0 {
			n = 0
		}
	}
	if n > len(rows) {
		n = len(rows)
	}
	return rows[:n]
}
