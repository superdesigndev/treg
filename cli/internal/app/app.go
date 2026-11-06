// Package app runs one CLI invocation and turns its outcome into an exit status.
package app

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"regexp"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/commands"
	"github.com/superdesigndev/treg/cli/internal/config"
	"github.com/superdesigndev/treg/cli/internal/output"
	"github.com/superdesigndev/treg/cli/internal/telemetry"
	"github.com/superdesigndev/treg/cli/internal/update"
)

// Exit statuses are a contract: 0 success, 1 a refusal or failed request, 2 a usage error.
const usageError = 2

func Main(argv []string, stderr io.Writer) int {
	api.UseSystemProxy()
	argv = append([]string(nil), argv...)
	started := time.Now()
	org, argv, err := popOrg(argv)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	json, argv := popJSON(argv)
	cfg := config.Load()
	env := commands.NewEnv(cfg, json, org)
	background := len(argv) > 0 && strings.HasPrefix(argv[0], "__")
	if !background {
		// Earlier commands' events ride this command's time on the network; nothing waits for them.
		go telemetry.Flush(context.Background(), api.RegistryURL(cfg))
		if exe, err := update.Executable(); err == nil {
			update.RemoveLeftover(exe)
			if len(argv) == 0 || argv[0] != "update" {
				update.MaybeStartBackground(cfg, exe)
			}
		}
	}
	root := commands.Root(env)
	root.SetArgs(argv)
	root.SetErr(stderr)
	ran, err := root.ExecuteC()
	code := status(err, ran, stderr)
	if !background {
		record(env, ran, err, code, time.Since(started))
	}
	return code
}

func status(err error, ran *cobra.Command, stderr io.Writer) int {
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
	path := "treg"
	if ran != nil {
		path = ran.CommandPath()
	}
	fmt.Fprintf(stderr, "treg: error: %v\nRun '%s --help' for usage.\n", err, path)
	return usageError
}

// pythonNames are the commands whose telemetry name, the Python CLI's handler name, differs from
// their path, so one command keeps one name across both CLIs.
var pythonNames = map[string]string{
	"":                      "home",
	"connections connect":   "oauth_connect",
	"connections providers": "oauth_providers",
	"scan":                  "import",
	"upload":                "import",
	"feedback submit":       "feedback",
	"usage":                 "usage_by_tag",
}

func commandName(ran *cobra.Command) string {
	path := ""
	if ran != nil {
		path = strings.TrimPrefix(strings.TrimPrefix(ran.CommandPath(), "treg"), " ")
	}
	if name, ok := pythonNames[path]; ok {
		return name
	}
	return strings.NewReplacer(" ", "_", "-", "_").Replace(path)
}

var optionName = regexp.MustCompile(`(--[A-Za-z][\w-]{0,40}|-[A-Za-z])`)

func record(env *commands.Env, ran *cobra.Command, err error, code int, took time.Duration) {
	notes := map[string]any{}
	for k, v := range env.Telemetry {
		notes[k] = v
	}
	name := commandName(ran)
	if v, ok := env.Telemetry["command"]; ok {
		name = v
		delete(notes, "command")
	}
	if code == usageError && err != nil {
		var exit *output.Exit
		if !errors.As(err, &exit) {
			name = "usage_error"
			if found := optionName.FindAllString(err.Error(), 10); len(found) > 0 {
				notes = map[string]any{"options": found}
			} else {
				notes = map[string]any{}
			}
		}
	}
	telemetry.Command(name, code, took, notes, env.Cfg.Channel)
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
