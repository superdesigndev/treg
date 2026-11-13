// Package app runs one CLI invocation and turns its outcome into an exit status.
package app

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
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

// startFlush sends earlier invocations' queued events while this one runs. Tests that read the
// queue replace it: the flush claims the queue file by renaming it while it sends.
var startFlush = func(registry string, transport http.RoundTripper) {
	go telemetry.Flush(context.Background(), registry, transport)
}

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
		startFlush(api.RegistryURL(cfg), api.Transport())
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
	if exit, ok := errors.AsType[*output.Exit](err); ok {
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

var (
	optionName     = regexp.MustCompile(`(?:^|[\s'"])(--[A-Za-z][\w-]{0,40}|-[A-Za-z])`)
	unknownCommand = regexp.MustCompile(`^unknown command "([^"]+)" for "[^"]+"`)
	invalidChoice  = regexp.MustCompile(`^argument <subcommand>: invalid choice: "([^"]+)"`)
)

// pythonOnly maps the Python CLI commands this binary lacks, and the programs `treg <program>`
// runs there, to their telemetry name: a usage error names one of these, never what was typed.
var pythonOnly = map[string]string{
	"add": "add", "admin": "admin", "agents": "agents", "cli": "cli", "hub": "hub", "onboard": "onboard",
	"run": "run", "runs": "runs", "serve": "serve", "setup-local-run": "setup_local_run", "shell": "shell",
	"with": "with",
	"bun":  "with", "cargo": "with", "claude": "with", "codex": "with", "cursor": "with", "deno": "with",
	"gemini": "with", "go": "with", "node": "with", "npm": "with", "npx": "with", "pnpm": "with",
	"python": "with", "python3": "with", "uv": "with", "yarn": "with",
	"org access": "org_access", "org archive": "org_archive", "org budget-rm": "org_budget_rm",
	"org budget-set": "org_budget_set", "org budgets": "org_budgets", "org delete": "org_delete",
	"org deny": "org_deny", "org deny-ls": "org_deny_ls", "org deny-rm": "org_deny_rm",
	"org invite": "org_invite", "org invites": "org_invites", "org leave": "org_leave",
	"org overflow": "org_overflow", "org pin": "org_pin", "org pins": "org_pins",
	"org project-new": "org_project_new", "org project-rm": "org_project_rm", "org projects": "org_projects",
	"org rename": "org_rename", "org revoke": "org_revoke", "org set-role": "org_set_role",
	"org unpin": "org_unpin",
	"skill add": "skill_add", "skill init": "skill_init", "skill push": "skill_push", "skill rm": "skill_rm",
	"skill scaffold": "skill_scaffold",
}

// missingCommand is the Python-only command a usage error asked for, or "".
func missingCommand(ran *cobra.Command, err error) string {
	m := unknownCommand.FindStringSubmatch(err.Error())
	if m == nil {
		m = invalidChoice.FindStringSubmatch(err.Error())
	}
	if m == nil || ran == nil {
		return ""
	}
	path := strings.TrimPrefix(strings.TrimPrefix(ran.CommandPath(), "treg"), " ")
	return pythonOnly[strings.TrimSpace(path+" "+m[1])]
}

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
		if _, ok := errors.AsType[*output.Exit](err); !ok {
			in := commandName(ran)
			name, notes = "usage_error", map[string]any{}
			var found []string
			for _, m := range optionName.FindAllStringSubmatch(err.Error(), 10) {
				found = append(found, m[1])
			}
			if len(found) > 0 {
				notes["options"] = found
			}
			if ran != nil && ran.HasParent() {
				notes["in_command"] = in
			}
			if missing := missingCommand(ran, err); missing != "" {
				notes["missing_command"] = missing
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
