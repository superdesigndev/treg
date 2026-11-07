package commands

import (
	"errors"
	"fmt"
	"io/fs"
	"math"
	"os"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/spf13/cobra"
)

// group is a command that only holds subcommands. Run bare, or with a word that is none of them,
// it is a usage error.
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

// choice refuses a value outside options as a usage error.
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

// intArg is an integer argument, else a usage error.
func intArg(name, s string) (int, error) {
	n, err := strconv.Atoi(s)
	if err != nil {
		return 0, fmt.Errorf("argument %s: invalid int value: %s", name, quote(s))
	}
	return n, nil
}

// idArg is a numeric id argument, spelled as the path segment the registry expects.
func idArg(name, s string) (string, error) {
	n, err := intArg(name, s)
	return strconv.Itoa(n), err
}

// countArg is an integer argument that cannot be negative.
func countArg(name, s string) (int, error) {
	n, err := intArg(name, s)
	if err == nil && n < 0 {
		return 0, fmt.Errorf("argument %s: must be 0 or more, got %d", name, n)
	}
	return n, err
}

// floatArg is a finite number argument, else a usage error.
func floatArg(name, s string) (float64, error) {
	f, err := strconv.ParseFloat(s, 64)
	if err != nil || math.IsInf(f, 0) || math.IsNaN(f) {
		return 0, fmt.Errorf("argument %s: invalid float value: %s", name, quote(s))
	}
	return f, nil
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

// readText is a UTF-8 text file with its line endings as "\n".
func readText(path string) (string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	if !utf8.Valid(raw) {
		return "", &fs.PathError{Op: "read", Path: path, Err: errors.New("not UTF-8 text")}
	}
	return strings.ReplaceAll(strings.ReplaceAll(string(raw), "\r\n", "\n"), "\r", "\n"), nil
}
