// Package app runs one CLI invocation and turns its outcome into an exit status.
package app

import (
	"errors"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/superdesigndev/treg/cli/internal/commands"
	"github.com/superdesigndev/treg/cli/internal/config"
	"github.com/superdesigndev/treg/cli/internal/output"
)

// Exit statuses are a contract: 0 success, 1 a refusal or failed request, 2 a usage error.
const usageError = 2

func Main(argv []string, stderr io.Writer) int {
	argv = append([]string(nil), argv...)
	org, argv, err := popOrg(argv)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	json, argv := popJSON(argv)
	env := commands.NewEnv(config.Load(), json, org)
	root := commands.Root(env)
	root.SetArgs(argv)
	root.SetErr(stderr)
	return status(root.Execute(), root.CommandPath(), stderr)
}

func status(err error, path string, stderr io.Writer) int {
	if err == nil {
		return 0
	}
	var exit *output.Exit
	if errors.As(err, &exit) {
		if exit.Message != "" {
			fmt.Fprintln(stderr, exit.Message)
		}
		return exit.Code
	}
	fmt.Fprintf(stderr, "treg: error: %v\nRun '%s --help' for usage.\n", err, path)
	return usageError
}

// popJSON removes the global --json, wherever it was typed: it lasts for this invocation only.
func popJSON(argv []string) (bool, []string) {
	for i, a := range argv {
		if a == "--json" {
			return true, append(argv[:i], argv[i+1:]...)
		}
	}
	return false, argv
}

// popOrg removes the global --org <slug> (or --org=<slug>).
func popOrg(argv []string) (string, []string, error) {
	for i, a := range argv {
		if a == "--org" {
			if i+1 >= len(argv) {
				return "", nil, errors.New("--org requires a value (an org slug)")
			}
			slug := argv[i+1]
			return slug, append(argv[:i], argv[i+2:]...), nil
		}
		if v, ok := strings.CutPrefix(a, "--org="); ok {
			return v, append(argv[:i], argv[i+1:]...), nil
		}
	}
	return "", argv, nil
}

func Run() {
	os.Exit(Main(os.Args[1:], os.Stderr))
}
