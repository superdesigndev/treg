package commands

import (
	"fmt"
	"os"
	"os/exec"
	"sort"
	"strings"
	"unicode"

	"github.com/superdesigndev/treg/cli/internal/agents"
	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/config"
	"github.com/superdesigndev/treg/cli/internal/output"
	"github.com/superdesigndev/treg/cli/internal/prompt"
	"github.com/superdesigndev/treg/cli/internal/providers"
	"github.com/superdesigndev/treg/cli/internal/pyjson"
)

type scannedCLI struct {
	entry    providers.Entry
	cli      *pyjson.Object
	decision providers.Decision
	envVar   string
}

type registeredCLI struct {
	name, tier, result string
}

func onPath(bin string) bool {
	_, err := exec.LookPath(bin)
	return err == nil
}

// uploadCLIs scans the machine for installed catalog CLIs and registers the ready ones: on the
// server tier when treg can hold the key, locally when the credential lives in the CLI's own config.
func (e *Env) uploadCLIs(f uploadFlags, envPath string) error {
	if f.add != "" {
		return e.addCLI(f)
	}
	catalog := e.uploadCatalog(f)
	envVals := map[string]string{}
	if isRegularFile(envPath) {
		text := readEnvText(envPath)
		envVals = providers.EnvValues(text, providers.VarNames(text))
	}
	value := func(name string) string {
		if name == "" {
			return ""
		}
		return firstString(os.Getenv(name), envVals[name])
	}
	var scanned []scannedCLI
	stop := prompt.Spin("checking this machine for installed CLIs")
	for _, entry := range catalog {
		cli := providers.Obj(entry, "cli")
		if cli.Len() == 0 || !providers.Truthy(cli, "bin") {
			continue
		}
		loggedIn := false
		for _, p := range providers.Strings(providers.Obj(cli, "detect"), "config_paths") {
			loggedIn = loggedIn || agents.Exists(config.ExpandUser(p))
		}
		envVar := providers.CLIEnvVar(cli)
		d := providers.ClassifyCLI(entry, onPath(cli.String("bin")), value(envVar) != "", loggedIn)
		scanned = append(scanned, scannedCLI{entry, cli, d, envVar})
	}
	stop()
	reportOnly := f.status || f.dryRun
	var ready []scannedCLI
	for _, s := range scanned {
		if s.decision.Status == "ready" {
			ready = append(ready, s)
		}
	}
	var registered []registeredCLI
	if len(ready) > 0 && !reportOnly {
		stop := prompt.Spin(fmt.Sprintf("registering %d ready CLI(s)", len(ready)))
		c := e.Client()
		existing, err := e.nameIDs(c, "/tools")
		if err != nil {
			stop()
			return err
		}
		for _, s := range ready {
			name := strings.ReplaceAll(s.cli.String("bin"), "_", "-")
			id, exists := existing[name]
			if exists && !f.replace {
				registered = append(registered, registeredCLI{name, s.decision.Tier, "exists"})
				continue
			}
			if exists {
				if err := e.remove(c, idPath("/tools/", id)); err != nil {
					stop()
					return err
				}
			}
			result, err := e.registerCLITool(c, s, value)
			if err != nil {
				stop()
				return err
			}
			registered = append(registered, registeredCLI{name, s.decision.Tier, result})
		}
		stop()
	}
	e.printCLIReport(scanned, registered, reportOnly, f.status)
	return nil
}

func (e *Env) registerCLITool(c *api.Client, s scannedCLI, value func(string) string) (string, error) {
	name := strings.ReplaceAll(s.cli.String("bin"), "_", "-")
	profile := pyjson.NewObject()
	for _, k := range s.cli.Keys() {
		if k != "verified" {
			v, _ := s.cli.Get(k)
			profile.Set(k, v)
		}
	}
	profile.Set("enabled", true)
	bindings := []any{}
	if s.decision.Tier == "server" {
		val := value(s.envVar)
		if val == "" {
			return "no value for " + orDash(s.envVar), nil
		}
		rs, err := e.post(c, "/secrets", pyjson.Ordered("name", name+"-key", "value", val, "kind", "env"))
		if err != nil {
			return "", err
		}
		if rs.Status >= 400 {
			return fmt.Sprintf("secret failed (%d)", rs.Status), nil
		}
		sid := secretID(rs.Body)
		if binding := providers.BuildBinding(providers.Obj(s.entry, "auth")); binding != nil {
			bindings = []any{withMember(binding, "secret_id", sid)}
		} else {
			inject, _ := profile.Get("inject")
			var out []any
			for _, item := range pyjson.List(inject) {
				if o, ok := item.(*pyjson.Object); ok {
					out = append(out, withMember(o, "secret_id", sid))
				}
			}
			if out == nil {
				out = []any{}
			}
			profile.Set("inject", out)
		}
	} else {
		// An explicit empty inject: the grant merges the catalog profile back in, and a missing key
		// would let the catalog's inject return.
		profile.Set("inject", []any{})
	}
	rt, err := e.post(c, "/tools", pyjson.Ordered("name", name, "base_url", s.entry.String("base_url"), "bindings", bindings, "cli", profile))
	if err != nil {
		return "", err
	}
	if rt.Status < 400 {
		return "ok", nil
	}
	return fmt.Sprintf("tool failed (%d) %s", rt.Status, clip(string(rt.Body), 80)), nil
}

func (e *Env) printCLIReport(scanned []scannedCLI, registered []registeredCLI, reportOnly, verbose bool) {
	buckets := map[string][]scannedCLI{}
	for _, s := range scanned {
		buckets[s.decision.Status] = append(buckets[s.decision.Status], s)
	}
	list := func(rows []scannedCLI) {
		var bins []string
		for _, r := range rows {
			bins = append(bins, r.cli.String("bin"))
		}
		sort.Strings(bins)
		for _, b := range bins {
			outln("  " + b)
		}
	}
	installed := 0
	for _, s := range []string{"ready", "needs_key", "needs_login", "unsupported"} {
		installed += len(buckets[s])
	}
	outf("Scanned %d catalog CLIs - %d installed here.\n\n", len(scanned), installed)
	var server, local []scannedCLI
	for _, r := range buckets["ready"] {
		if r.decision.Tier == "server" {
			server = append(server, r)
		} else if r.decision.Tier == "local" {
			local = append(local, r)
		}
	}
	verb := "Registered"
	if reportOnly {
		verb = "Would register"
	}
	if len(server) > 0 {
		outln(verb + " (server, key injected):")
		list(server)
	}
	if len(local) > 0 {
		outln(verb + " (local, uses your login):")
		list(local)
	}
	if !reportOnly {
		for _, r := range registered {
			if r.result == "ok" || r.result == "exists" {
				outf("  ↗ %s\n", e.detailURL("tool", r.name))
			}
		}
	}
	if len(buckets["needs_key"]) > 0 || len(buckets["needs_login"]) > 0 {
		outln("\nNeeds setup before it can register:")
		for _, r := range buckets["needs_key"] {
			alt := ""
			if r.decision.Login != "" {
				alt = " (or run: " + r.decision.Login + ")"
			}
			outf("  %s: set %s in your env%s\n", r.cli.String("bin"), firstString(r.decision.Env, "the API key"), alt)
		}
		for _, r := range buckets["needs_login"] {
			outf("  %s: %s\n", r.cli.String("bin"), r.decision.Action)
		}
		outln("  then re-run: treg upload clis")
	}
	if len(buckets["unsupported"]) > 0 {
		outln("\nNot supported:")
		for _, r := range buckets["unsupported"] {
			outf("  %s: %s\n", r.cli.String("bin"), r.decision.Reason)
		}
	}
	var failed []registeredCLI
	for _, r := range registered {
		if r.result != "ok" && r.result != "exists" {
			failed = append(failed, r)
		}
	}
	if len(failed) > 0 {
		outln("\nFailed to register:")
		for _, r := range failed {
			outf("  %s: %s\n", r.name, r.result)
		}
	}
	notInstalled := buckets["not_installed"]
	if len(notInstalled) > 0 && verbose {
		outln("\nIn the catalog, not installed here:")
		for _, r := range notInstalled {
			outln(strings.TrimRightFunc(fmt.Sprintf("  %-12s %s", r.cli.String("bin"), r.decision.Action), unicode.IsSpace))
		}
	} else if len(notInstalled) > 0 {
		outf("\n%d more catalog CLIs aren't installed. List them with: treg scan clis --status\n", len(notInstalled))
	}
	if !reportOnly && len(registered) > 0 {
		done := 0
		for _, r := range registered {
			if r.result == "ok" {
				done++
			}
		}
		outf("\nRegistered %d CLI tool(s). Run one with: treg cli run <name>\n", done)
	}
}

// titleCase capitalizes each word and lowercases the rest of it.
func titleCase(s string) string {
	var b strings.Builder
	prevCased := false
	for _, r := range s {
		cased := unicode.IsUpper(r) || unicode.IsLower(r) || unicode.IsTitle(r)
		switch {
		case cased && !prevCased:
			b.WriteRune(unicode.ToTitle(r))
		case cased:
			b.WriteRune(unicode.ToLower(r))
		default:
			b.WriteRune(r)
		}
		prevCased = cased
	}
	return b.String()
}

// addCLI registers an installed CLI the catalog does not know, and prints the catalog entry to
// share so it can be added for everyone.
func (e *Env) addCLI(f uploadFlags) error {
	bin := strings.TrimSpace(f.add)
	if !onPath(bin) {
		return output.Fail("'%s' is not on your PATH - install it first (or check the name).", bin)
	}
	for _, entry := range e.loadProviderCatalog() {
		if providers.Obj(entry, "cli").String("bin") == bin {
			return output.Fail("'%s' is already in the catalog - just run `treg upload clis`.", bin)
		}
	}
	envVar := f.envVar
	if !f.envVarSet {
		if !prompt.StdinIsTerminal() {
			return output.Fail("non-interactive: pass --env VAR (the variable %s reads its key from; --env '' when it signs in "+
				"with its own login) and --base-url URL", bin)
		}
		line, err := prompt.Line(fmt.Sprintf("Env var %s reads its key from (blank = it logs in via its own config): ", bin))
		if err != nil {
			return output.Fail("treg: no answer on stdin")
		}
		envVar = strings.TrimSpace(line)
	}
	baseURL := f.baseURL
	if baseURL == "" {
		if !prompt.StdinIsTerminal() {
			return output.Fail("non-interactive: pass --base-url URL (the provider API %s calls)", bin)
		}
		line, err := prompt.Line(fmt.Sprintf("Provider API base_url for %s (e.g. https://api.example.com): ", bin))
		if err != nil {
			return output.Fail("treg: no answer on stdin")
		}
		baseURL = strings.TrimSpace(line)
	}
	if baseURL == "" {
		return output.Fail("a base_url is required to register the tool (a CLI with no HTTP API isn't supported via --add yet).")
	}
	name := strings.ReplaceAll(bin, "_", "-")
	mech := "config_file"
	if envVar != "" {
		mech = "env"
	}
	profile := pyjson.Ordered("bin", bin, "enabled", true, "auth_mechanism", mech)
	c := e.Client()
	existing, err := e.nameIDs(c, "/tools")
	if err != nil {
		return err
	}
	if id, ok := existing[name]; ok {
		if !f.replace {
			return output.Fail("a tool named '%s' already exists (use --replace).", name)
		}
		if err := e.remove(c, idPath("/tools/", id)); err != nil {
			return err
		}
	}
	bindings := []any{}
	if envVar != "" {
		if val := os.Getenv(envVar); val != "" {
			rs, err := e.post(c, "/secrets", pyjson.Ordered("name", name+"-key", "value", val, "kind", "env"))
			if err != nil {
				return err
			}
			if rs.Status >= 400 {
				return output.Fail("secret failed (%d) %s", rs.Status, clip(string(rs.Body), 100))
			}
			sid := secretID(rs.Body)
			profile.Set("inject", []any{pyjson.Ordered("via", "env", "name", envVar, "secret_id", sid)})
			bindings = []any{pyjson.Ordered("via", "header", "name", "Authorization", "format", "Bearer {secret}", "secret_id", sid)}
		} else {
			profile.Set("inject", []any{pyjson.Ordered("via", "env", "name", envVar)})
			outf("  note: %s isn't set - the tool is registered; set it before running server-side.\n", envVar)
		}
	}
	rt, err := e.post(c, "/tools", pyjson.Ordered("name", name, "base_url", baseURL, "bindings", bindings, "cli", profile))
	if err != nil {
		return err
	}
	if rt.Status >= 400 {
		return output.Fail("tool failed (%d) %s", rt.Status, clip(string(rt.Body), 120))
	}
	outf("✓ Registered '%s'. Run it locally: `treg cli run %s`.\n", name, name)
	outf("  ↗ %s\n", e.detailURL("tool", name))
	if len(bindings) > 0 {
		outf("  (key stored - also a callable API tool; to run '%s' on the SERVER too, an admin adds it to TREG_RUN_ALLOWED_BINS.)\n", bin)
	}
	tokens := []any{}
	cli := pyjson.Ordered("bin", bin, "auth_mechanism", mech)
	if envVar != "" {
		tokens = []any{strings.Split(envVar, "_")[0]}
		cli.Set("inject", []any{pyjson.Ordered("via", "env", "name", envVar)})
	}
	entry := pyjson.Ordered("provider", titleCase(name), "tokens", tokens, "base_url", baseURL,
		"auth", pyjson.Ordered("shape", "bearer"), "cli", cli)
	outln("\nShare this to add it to the catalog for everyone:\n  " + pyjson.Dumps(entry, pyjson.Options{Indent: -1, EnsureASCII: true}))
	return nil
}
