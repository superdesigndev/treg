package commands

import (
	"bytes"
	"crypto/sha1"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"regexp"
	"slices"
	"sort"
	"strings"
	"time"
	"unicode"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/agents"
	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/config"
	"github.com/superdesigndev/treg/cli/internal/output"
	"github.com/superdesigndev/treg/cli/internal/prompt"
	"github.com/superdesigndev/treg/cli/internal/providers"
	"github.com/superdesigndev/treg/cli/internal/pyjson"
	"github.com/superdesigndev/treg/cli/internal/skillscan"
)

const (
	llmDefaultBase  = "https://generativelanguage.googleapis.com/v1beta/openai"
	llmDefaultModel = "gemini-2.5-flash"
)

type uploadFlags struct {
	mode, dir, envFile, skillsDir, selectNames string
	all, replace, noOAuth, llm, dryRun, status bool
	llmToken, llmModel, llmBaseURL             string
	add, envVar, baseURL                       string
	envVarSet                                  bool
	scan, alias                                bool
}

var uploadModes = []string{"env", "skills", "clis"}

func modeArg(cmd *cobra.Command, args []string) error {
	if len(args) > 1 {
		return fmt.Errorf("unrecognized arguments: %s", strings.Join(args[1:], " "))
	}
	if len(args) == 1 && !slices.Contains(uploadModes, args[0]) {
		return fmt.Errorf("argument mode: invalid choice: %s (choose from 'env', 'skills', 'clis')", pyRepr(args[0]))
	}
	return nil
}

func addLocationFlags(cmd *cobra.Command, f *uploadFlags, selectHelp string) {
	fl := cmd.Flags()
	fl.StringVar(&f.dir, "dir", "", "base directory (default: cwd): its .env and its skill subdirs")
	fl.StringVar(&f.envFile, "env-file", "", "explicit path to the env file (overrides --dir/.env)")
	fl.StringVar(&f.skillsDir, "skills-dir", "", "explicit skills directory (overrides --dir)")
	fl.StringVar(&f.selectNames, "select", "", selectHelp)
}

func addCLIFlags(cmd *cobra.Command, f *uploadFlags) {
	fl := cmd.Flags()
	fl.BoolVar(&f.status, "status", false, "clis: scan + report only, register nothing")
	fl.StringVar(&f.add, "add", "", "clis: register an INSTALLED cli that's not in the catalog (prompts for its key env var + API base_url)")
	fl.StringVar(&f.envVar, "env", "", "clis --add: env var the cli reads its key from (blank = it authenticates via its own login/config)")
	fl.StringVar(&f.baseURL, "base-url", "", "clis --add: the provider's API base_url for the tool")
}

func scanCmd(env *Env) *cobra.Command {
	f := uploadFlags{scan: true, dryRun: true, all: true, noOAuth: true, llmModel: llmDefaultModel, llmBaseURL: llmDefaultBase}
	cmd := &cobra.Command{
		Use:   "scan [env|skills|clis]",
		Short: "Scan a directory / machine (read-only): list the keys, skills & CLIs treg would upload. Nothing leaves this machine.",
		Example: "  treg scan                                      # both .env + skills in this dir\n" +
			"  treg scan env                                  # just the .env keys\n" +
			"  treg scan clis                                 # installed CLIs treg can register\n" +
			"  treg scan skills --dir ~/.claude/skills",
		Args: modeArg,
		RunE: func(cmd *cobra.Command, args []string) error {
			return env.runUpload(cmd, args, f)
		},
	}
	addLocationFlags(cmd, &f, "comma-separated names to show (else everything)")
	addCLIFlags(cmd, &f)
	return cmd
}

func uploadLikeCmd(env *Env, use, short, example string, alias bool) *cobra.Command {
	f := uploadFlags{alias: alias}
	cmd := &cobra.Command{
		Use:     use + " [env|skills|clis]",
		Short:   short,
		Example: example,
		Hidden:  alias,
		Args:    modeArg,
		RunE: func(cmd *cobra.Command, args []string) error {
			return env.runUpload(cmd, args, f)
		},
	}
	addLocationFlags(cmd, &f, "comma-separated names to upload (else interactive)")
	fl := cmd.Flags()
	fl.BoolVar(&f.all, "all", false, "upload everything detected without prompting")
	fl.BoolVar(&f.replace, "replace", false, "delete-then-recreate anything already registered (re-run safe)")
	fl.BoolVar(&f.noOAuth, "no-oauth", false, "skip the per-provider OAuth connect prompts")
	fl.BoolVar(&f.llm, "llm", false, "resolve UNKNOWN keys with an LLM (OpenAI-compatible)")
	fl.StringVar(&f.llmToken, "llm-token", "", "LLM API token (or set TREG_LLM_TOKEN)")
	fl.StringVar(&f.llmModel, "llm-model", llmDefaultModel, "LLM model (default: "+llmDefaultModel+")")
	fl.StringVar(&f.llmBaseURL, "llm-base-url", llmDefaultBase, "OpenAI-compatible base URL (default: Gemini)")
	addCLIFlags(cmd, &f)
	fl.BoolVar(&f.dryRun, "dry-run", false, "preview only; `treg scan` is the same")
	_ = fl.MarkHidden("dry-run")
	return cmd
}

// uploadCmds is `upload` and its hidden old name `import`.
func uploadCmds(env *Env) []*cobra.Command {
	upload := uploadLikeCmd(env, "upload",
		"Upload a directory / machine: register its .env provider keys, skill subdirs, AND/OR installed CLIs (encrypted server-side).",
		"  treg upload                                    # both .env + skills in this dir\n"+
			"  treg upload env --select openai,stripe        # just picked provider keys\n"+
			"  treg upload clis                               # register installed catalog CLIs\n"+
			"  treg upload skills --dir ~/.claude/skills --all\n"+
			"  treg scan                                      # preview first; nothing leaves the machine",
		false)
	legacy := uploadLikeCmd(env, "import", "(deprecated) old name for `treg upload`.", "", true)
	return []*cobra.Command{scanCmd(env), upload, legacy}
}

func (e *Env) runUpload(cmd *cobra.Command, args []string, f uploadFlags) error {
	if len(args) == 1 {
		f.mode = args[0]
	}
	f.envVarSet = cmd.Flags().Changed("env")
	if f.alias {
		errln("note: `treg import` is now `treg upload` (same command; preview with `treg scan`)")
	}
	cwd := workingDir()
	baseDir := firstString(f.dir, cwd)
	envPath := firstString(f.envFile, filepath.Join(baseDir, ".env"))
	skillsDir := firstString(f.skillsDir, baseDir)
	haveEnv := isRegularFile(envPath)
	haveSkills := false
	if isDirectory(skillsDir) {
		haveSkills = skillscan.IsSkillDir(skillsDir)
		if entries, err := os.ReadDir(skillsDir); err == nil && !haveSkills {
			for _, c := range entries {
				p := filepath.Join(skillsDir, c.Name())
				if isDirectory(p) && skillscan.IsSkillDir(p) {
					haveSkills = true
					break
				}
			}
		}
	}
	ran := false
	if f.mode == "" || f.mode == "env" {
		if haveEnv {
			if err := e.uploadEnv(f, envPath); err != nil {
				return err
			}
			ran = true
		} else if f.mode == "env" {
			return output.Fail("no .env at %s (use --dir DIR or --env-file FILE)", envPath)
		}
	}
	if f.mode == "" || f.mode == "clis" {
		if ran {
			outln()
		}
		if err := e.uploadCLIs(f, envPath); err != nil {
			return err
		}
		ran = true
	}
	if f.mode == "" || f.mode == "skills" {
		if haveSkills {
			if ran {
				outln()
			}
			// A skill's env credential lives in the project .env, which may sit above the skills dir.
			skillsEnv := envPath
			if !isRegularFile(envPath) {
				skillsEnv = firstString(f.envFile, findEnvUpwards(skillsDir, cwd), envPath)
			}
			if err := e.uploadSkills(f, skillsDir, skillsEnv, cwd); err != nil {
				return err
			}
			ran = true
		} else if f.mode == "skills" {
			return output.Fail("no skills (subdirs with SKILL.md) under %s%s", skillsDir, agentSkillsHint(skillsDir))
		}
	}
	if !ran {
		verb := "upload"
		if f.scan {
			verb = "scan"
		}
		return output.Fail("nothing to %s in %s: no .env, no skill subdirs. Use --dir / --skills-dir.%s", verb, baseDir, agentSkillsHint(baseDir))
	}
	return nil
}

// workingDir is the current directory with symlinks resolved, as the kernel reports it.
func workingDir() string {
	wd, err := os.Getwd()
	if err != nil {
		return "."
	}
	if real, err := filepath.EvalSymlinks(wd); err == nil {
		return real
	}
	return wd
}

func isRegularFile(p string) bool {
	info, err := os.Stat(p)
	return err == nil && info.Mode().IsRegular()
}

func isDirectory(p string) bool {
	info, err := os.Stat(p)
	return err == nil && info.IsDir()
}

func absFrom(cwd, p string) string {
	if filepath.IsAbs(p) {
		return filepath.Clean(p)
	}
	return filepath.Join(cwd, p)
}

// findEnvUpwards is the nearest .env walking up from start, at most eight levels.
func findEnvUpwards(start, cwd string) string {
	d := absFrom(cwd, start)
	for range 8 {
		cand := filepath.Join(d, ".env")
		if isRegularFile(cand) {
			return cand
		}
		parent := filepath.Dir(d)
		if parent == d {
			break
		}
		d = parent
	}
	return ""
}

// agentSkillsHint names where the skills actually are when the scanned dir has none.
func agentSkillsHint(baseDir string) string {
	home, _ := os.UserHomeDir()
	for _, d := range []string{filepath.Join(baseDir, ".claude", "skills"), filepath.Join(baseDir, ".agents", "skills"),
		filepath.Join(home, ".claude", "skills"), filepath.Join(home, ".agents", "skills")} {
		n := 0
		if entries, err := os.ReadDir(d); err == nil {
			for _, c := range entries {
				p := filepath.Join(d, c.Name())
				if isDirectory(p) && skillscan.IsSkillDir(p) {
					n++
				}
			}
		}
		if n > 0 {
			return fmt.Sprintf("\nfound %d skill(s) under %s - scan them with: treg scan skills --skills-dir %s", n, d, d)
		}
	}
	return ""
}

// loadProviderCatalog is the registry's GET /providers.json, cached per registry, when it is at
// least as new as the catalog this build carries; else the cache under the same rule; else the
// bundled one. A CLI newer than its registry never falls back to an older catalog.
func (e *Env) loadProviderCatalog() []providers.Entry {
	version, bundled := providers.Bundled()
	sum := sha1.Sum([]byte(e.Registry()))
	cache := config.Sibling("providers-cache-" + hex.EncodeToString(sum[:])[:10] + ".json")
	newer := func(root *pyjson.Object) bool {
		n, ok := providers.IntOK(root, "version")
		return ok && n >= version
	}
	stop := prompt.Spin("refreshing the provider catalog")
	r, err := e.AnonymousClient().Do(api.Request{Path: "/providers.json"})
	stop()
	if err == nil && r.Status == 200 {
		if v, err := pyjson.Decode(r.Body); err == nil {
			if root, ok := v.(*pyjson.Object); ok && providers.Truthy(root, "providers") {
				if os.MkdirAll(filepath.Dir(cache), 0o777) == nil {
					_ = os.WriteFile(cache, r.Body, 0o666)
				}
				if newer(root) {
					return providers.Entries(root)
				}
			}
		}
	}
	if raw, err := os.ReadFile(cache); err == nil {
		if v, err := pyjson.Decode(raw); err == nil {
			if root, ok := v.(*pyjson.Object); ok && providers.Truthy(root, "providers") && newer(root) {
				return providers.Entries(root)
			}
		}
	}
	return bundled
}

func (e *Env) uploadCatalog(f uploadFlags) []providers.Entry {
	if f.dryRun {
		_, bundled := providers.Bundled()
		return bundled
	}
	return e.loadProviderCatalog()
}

// detailURL is the shareable dashboard page of a registered skill or tool.
func (e *Env) detailURL(kind, name string) string {
	section := "tools"
	if kind == "skill" {
		section = "skills"
	}
	return strings.TrimRight(e.Registry(), "/") + "/app/" + section + "/" + quoteAll(name)
}

func selected(want string) map[string]bool {
	out := map[string]bool{}
	for _, s := range strings.Split(want, ",") {
		out[strings.ToLower(strings.TrimSpace(s))] = true
	}
	return out
}

var errNonInteractive = output.Fail("non-interactive: pass --all to import everything, --select to choose, or --dry-run to preview")

func (e *Env) selectProviders(supported []*providers.Action, f uploadFlags) ([]*providers.Action, error) {
	if f.selectNames != "" {
		want := selected(f.selectNames)
		var out []*providers.Action
		for _, a := range supported {
			if want[a.ToolName] || want[strings.ToLower(a.Detection.Provider)] {
				out = append(out, a)
			}
		}
		return out, nil
	}
	if f.all || f.dryRun {
		return supported, nil
	}
	if !prompt.StdinIsTerminal() {
		return nil, errNonInteractive
	}
	choices := make([]prompt.Choice, len(supported))
	for i, a := range supported {
		choices[i] = prompt.Choice{Title: fmt.Sprintf("%-14s %s", a.ToolName, a.BaseURL), Checked: true}
	}
	picked, _ := prompt.Checkbox("Providers detected in your env - register which as tools?", choices)
	var out []*providers.Action
	for _, i := range picked {
		out = append(out, supported[i])
	}
	return out, nil
}

func readEnvText(path string) string {
	raw, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return string(raw)
}

func (e *Env) nameIDs(c *api.Client, path string) (map[string]any, error) {
	r, err := e.do(c, api.Request{Path: path})
	if err != nil {
		return nil, err
	}
	out := map[string]any{}
	if r.Status != 200 {
		return out, nil
	}
	var rows []map[string]any
	_ = json.Unmarshal(r.Body, &rows)
	for _, row := range rows {
		out[str(row["name"])] = row["id"]
	}
	return out, nil
}

func idPath(prefix string, id any) string { return prefix + str(id) }

func secretID(body []byte) any {
	var v map[string]any
	d := json.NewDecoder(bytes.NewReader(body))
	d.UseNumber()
	_ = d.Decode(&v)
	if id, ok := v["id"]; ok && pyjson.Truthy(id) {
		return id
	}
	return v["secret_id"]
}

func (e *Env) post(c *api.Client, path string, body any) (*api.Response, error) {
	return e.do(c, api.Request{Method: "POST", Path: path, Body: []byte(pyjson.Dumps(body, pyjson.Options{Indent: -1})), Header: jsonHeader})
}

func (e *Env) remove(c *api.Client, path string) error {
	_, err := e.do(c, api.Request{Method: "DELETE", Path: path})
	return err
}

func withMember(o *pyjson.Object, key string, value any) *pyjson.Object {
	c := o.Copy()
	c.Set(key, value)
	return c
}

func (e *Env) uploadEnv(f uploadFlags, envPath string) error {
	catalog := e.uploadCatalog(f)
	text := readEnvText(envPath)
	dets := providers.ScanEnv(text, catalog)
	actions := providers.PlanActions(dets)
	var supported, deferred []*providers.Action
	for _, a := range actions {
		switch {
		case a.Supported:
			supported = append(supported, a)
		case a.Detection.Kind == "oauth_pair" && providers.OAuthReady(a.Detection):
		default:
			deferred = append(deferred, a)
		}
	}
	var oauthDets, unknowns []providers.Detection
	for _, d := range dets {
		if d.Kind == "oauth_pair" && providers.OAuthReady(d) {
			oauthDets = append(oauthDets, d)
		}
		if d.Kind == "unknown_secret" {
			unknowns = append(unknowns, d)
		}
	}
	outf("Scanned %s: %d key(s) to register, %d OAuth, %d other.\n\n", envPath, len(supported), len(oauthDets), len(deferred))
	var chosen []*providers.Action
	if len(supported) > 0 {
		picked, err := e.selectProviders(supported, f)
		if err != nil {
			return err
		}
		chosen = picked
	}
	if f.selectNames != "" && len(supported) > 0 && len(chosen) == 0 {
		outf("  (no detected provider key matched --select %s)\n", f.selectNames)
	}
	if f.dryRun {
		if len(chosen) > 0 {
			if f.scan {
				outln("Found - `treg upload` registers these:")
			} else {
				outln("DRY RUN - would register:")
			}
			for _, a := range chosen {
				outf("  ✓ %-14s %s   [%s: %s]  (secret %s)\n", a.ToolName, a.BaseURL,
					pyNone(a.Binding.String("name")), pyNone(a.Binding.String("format")), a.SecretName)
			}
		}
		if len(oauthDets) > 0 {
			if f.scan {
				outln("\nFound OAuth pairs - `treg upload` connects them one by one:")
			} else {
				outln("\nDRY RUN - would prompt to connect one by one:")
			}
			for _, d := range oauthDets {
				outf("  ◆ %-14s %s\n", d.Provider, strings.Join(d.Vars, " + "))
			}
		}
		if f.llm && len(unknowns) > 0 {
			var names []string
			for _, d := range unknowns {
				names = append(names, d.Vars[0])
			}
			outf("\nDRY RUN - would ask %s to resolve: %s\n", f.llmModel, strings.Join(names, ", "))
		}
		showSkipped(deferred)
		return nil
	}
	c := e.Client()
	if len(chosen) > 0 {
		var need []string
		for _, a := range chosen {
			if a.Combine != nil {
				need = append(need, a.Combine...)
			} else if a.SecretName != "" {
				need = append(need, a.SecretName)
			}
		}
		values := providers.EnvValues(text, need)
		tools, err := e.nameIDs(c, "/tools")
		if err != nil {
			return err
		}
		secrets, err := e.nameIDs(c, "/secrets")
		if err != nil {
			return err
		}
		ok := 0
		for _, a := range chosen {
			var val string
			if a.Combine != nil {
				u, pw := values[a.Combine[0]], values[a.Combine[1]]
				if u == "" || pw == "" {
					outf("  ✗ %s: %s/%s missing in the env - skipped\n", a.ToolName, a.Combine[0], a.Combine[1])
					continue
				}
				val = base64.StdEncoding.EncodeToString([]byte(u + ":" + pw))
			} else {
				val = values[a.SecretName]
				if val == "" {
					outf("  ✗ %s: %s has no value in the env - skipped\n", a.ToolName, a.SecretName)
					continue
				}
			}
			if id, exists := tools[a.ToolName]; exists {
				if !f.replace {
					outf("  · %s: already registered (use --replace)\n", a.ToolName)
					outf("    ↗ %s\n", e.detailURL("tool", a.ToolName))
					continue
				}
				if err := e.remove(c, idPath("/tools/", id)); err != nil {
					return err
				}
				if sid, ok := secrets[a.SecretName]; ok {
					if err := e.remove(c, idPath("/secrets/", sid)); err != nil {
						return err
					}
				}
			} else if sid, ok := secrets[a.SecretName]; ok && f.replace {
				if err := e.remove(c, idPath("/secrets/", sid)); err != nil {
					return err
				}
			}
			rs, err := e.post(c, "/secrets", pyjson.Ordered("name", a.SecretName, "value", val, "kind", "env"))
			if err != nil {
				return err
			}
			if rs.Status >= 400 {
				outf("  ✗ %s: secret failed (%d) %s\n", a.ToolName, rs.Status, clip(string(rs.Body), 100))
				continue
			}
			sid := secretID(rs.Body)
			bindings := []any{withMember(a.Binding, "secret_id", sid)}
			for _, name := range a.RequiredHeaders.Keys() {
				value, _ := a.RequiredHeaders.Get(name)
				bindings = append(bindings, pyjson.Ordered("secret_id", sid, "injector", "env", "location", "header",
					"name", name, "format", value))
			}
			body := pyjson.Ordered("name", a.ToolName, "base_url", a.BaseURL, "bindings", bindings)
			if a.Health != nil {
				body.Set("health_check", a.Health)
			}
			rt, err := e.post(c, "/tools", body)
			if err != nil {
				return err
			}
			if rt.Status >= 400 {
				outf("  ✗ %s: tool failed (%d) %s\n", a.ToolName, rt.Status, clip(string(rt.Body), 100))
				continue
			}
			outf("  ✓ %-14s %s\n", a.ToolName, a.BaseURL)
			outf("    ↗ %s\n", e.detailURL("tool", a.ToolName))
			ok++
		}
		outf("\nRegistered %d/%d tools.\n", ok, len(chosen))
	}
	if len(oauthDets) > 0 {
		if !f.noOAuth && prompt.StdinIsTerminal() {
			if err := e.oauthLoop(c, oauthDets, text); err != nil {
				return err
			}
		} else {
			var provs []string
			for _, d := range oauthDets {
				provs = append(provs, firstString(d.Provider, "?"))
			}
			outf("\n%d OAuth app(s) detected (%s) - not connected. Connect when ready:  treg connections connect <name>\n",
				len(oauthDets), strings.Join(provs, ", "))
		}
	}
	if f.llm && len(unknowns) > 0 {
		if err := e.uploadLLM(c, unknowns, text, f); err != nil {
			return err
		}
	}
	showSkipped(deferred)
	return nil
}

func showSkipped(skipped []*providers.Action) {
	if len(skipped) == 0 {
		return
	}
	outln("\nNot auto-registered (need another path):")
	for _, a := range skipped {
		outf("  · %s  -  %s\n", strings.Join(a.Detection.Vars, " + "), a.Reason)
	}
}

// oauthLoop connects the detected OAuth apps one at a time, each after a yes on the terminal.
func (e *Env) oauthLoop(c *api.Client, dets []providers.Detection, envText string) error {
	var need []string
	for _, d := range dets {
		cid, csec := providers.OAuthParts(d.Vars)
		for _, v := range []string{cid, csec} {
			if v != "" {
				need = append(need, v)
			}
		}
	}
	vals := providers.EnvValues(envText, need)
	outf("\n%d OAuth provider(s) to connect (each opens a browser consent):\n", len(dets))
	for i, d := range dets {
		ans, err := prompt.Line(fmt.Sprintf("\n  OAuth %d/%d: connect %s (%s)? [y = connect / n = skip / a = skip all]: ",
			i+1, len(dets), d.Provider, strings.Join(d.Vars, " + ")))
		if err != nil {
			ans = "a"
		}
		ans = strings.ToLower(strings.TrimSpace(ans))
		if ans == "a" || ans == "all" || ans == "skip-all" {
			outln("  · skipping all remaining OAuth.")
			break
		}
		if ans != "y" && ans != "yes" {
			outf("  · %s: skipped.\n", d.Provider)
			continue
		}
		if err := e.oauthConnect(c, d, vals); err != nil {
			return err
		}
	}
	return nil
}

func (e *Env) oauthConnect(c *api.Client, d providers.Detection, vals map[string]string) error {
	cid, csec := providers.OAuthParts(d.Vars)
	scopes, ok := d.Auth.Get("scopes")
	if !ok {
		scopes = []any{}
	}
	value := func(k string) any {
		if v, ok := vals[k]; ok {
			return v
		}
		return nil
	}
	body := pyjson.Ordered("name", providers.Slug(d.Provider), "client_id", value(cid), "client_secret", value(csec),
		"auth_uri", d.Auth.String("auth_uri"), "token_uri", d.Auth.String("token_uri"), "scopes", scopes)
	r, err := e.post(c, "/oauth/start", body)
	if err != nil {
		return err
	}
	if r.Status != 200 {
		outf("  ✗ %s: /oauth/start failed (%d) %s\n", d.Provider, r.Status, clip(string(r.Body), 100))
		return nil
	}
	var start map[string]any
	_ = json.Unmarshal(r.Body, &start)
	redirect, rOK := start["redirect_uri"].(string)
	consent, cOK := start["consent_url"].(string)
	state, sOK := start["state"].(string)
	if !rOK || !cOK || !sOK {
		outf("  ✗ %s: unexpected /oauth/start response - skipped\n", d.Provider)
		return nil
	}
	outf("    1. Ensure this redirect URI is allowed in the %s OAuth app:\n       %s\n", d.Provider, redirect)
	outf("    2. Open to authorize:\n       %s\n    Waiting… (Ctrl-C to skip this one)\n", consent)
	interrupt := make(chan os.Signal, 1)
	signal.Notify(interrupt, os.Interrupt)
	defer signal.Stop(interrupt)
	for range 150 {
		select {
		case <-interrupt:
			outf("\n  · %s: skipped (Ctrl-C)\n", d.Provider)
			return nil
		case <-time.After(2 * time.Second):
		}
		s, err := c.Do(api.Request{Path: "/oauth/status/" + state})
		if err != nil {
			continue
		}
		var st map[string]any
		if json.Unmarshal(s.Body, &st) != nil {
			continue
		}
		switch st["status"] {
		case "done":
			outf("  ✓ %s connected (oauth secret id %s)\n", d.Provider, pyNone(str(st["secret_id"])))
			return nil
		case "error":
			outf("  ✗ %s: %s\n", d.Provider, pyNone(str(st["detail"])))
			return nil
		}
	}
	outf("  ✗ %s: timed out waiting for authorization.\n", d.Provider)
	return nil
}

var llmHTTP = &http.Client{Timeout: 60 * time.Second}

// llmChat is one OpenAI-compatible chat completion, the provider's own key as the bearer.
func llmChat(base, token, model, system, user string) (string, error) {
	body := pyjson.Ordered("model", model, "temperature", 0, "messages", []any{
		pyjson.Ordered("role", "system", "content", system), pyjson.Ordered("role", "user", "content", user)})
	req, err := http.NewRequest(http.MethodPost, strings.TrimRight(base, "/")+"/chat/completions",
		strings.NewReader(pyjson.Dumps(body, pyjson.Options{Indent: -1})))
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := llmHTTP.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return "", fmt.Errorf("HTTP %d from %s", resp.StatusCode, req.URL)
	}
	var out struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return "", err
	}
	if len(out.Choices) == 0 {
		return "", fmt.Errorf("the reply has no choices")
	}
	return out.Choices[0].Message.Content, nil
}

// uploadLLM asks a model for the unknown keys' providers, then registers each only after a yes.
func (e *Env) uploadLLM(c *api.Client, unknowns []providers.Detection, envText string, f uploadFlags) error {
	token := firstString(f.llmToken, os.Getenv("TREG_LLM_TOKEN"))
	if token == "" {
		outln("\n[--llm needs an API token: pass --llm-token <key> or set TREG_LLM_TOKEN]")
		return nil
	}
	var names []string
	for _, d := range unknowns {
		names = append(names, d.Vars[0])
	}
	system, user := providers.LLMPrompt(names)
	outf("\nAsking %s to resolve %d unknown key(s)…\n", f.llmModel, len(names))
	text, err := llmChat(f.llmBaseURL, token, f.llmModel, system, user)
	if err != nil {
		outf("  ✗ LLM call failed: %v\n", err)
		return nil
	}
	resolvedRows := providers.LLMParse(text)
	if len(resolvedRows) == 0 {
		outln("  · the LLM returned no confident matches.")
		return nil
	}
	vals := providers.EnvValues(envText, names)
	tools, err := e.nameIDs(c, "/tools")
	if err != nil {
		return err
	}
	secrets, err := e.nameIDs(c, "/secrets")
	if err != nil {
		return err
	}
	used := map[string]bool{}
	for k := range tools {
		used[k] = true
	}
	for _, row := range resolvedRows {
		v, base, auth := row.String("var"), row.String("base_url"), providers.Obj(row, "auth")
		binding := providers.BuildBinding(auth)
		if binding == nil {
			outf("  · %s: unsupported auth shape %s - skipped\n", v, auth.String("shape"))
			continue
		}
		hdr := ""
		if auth.String("shape") != "bearer" {
			hdr = " " + pyNone(firstString(auth.String("header"), auth.String("param")))
		}
		ans := "n"
		question := fmt.Sprintf("  LLM: %s → %s %s  [%s%s]. Register? [y/N]: ", v, pyNone(row.String("provider")), base, auth.String("shape"), hdr)
		if prompt.StdinIsTerminal() {
			if line, err := prompt.Line(question); err == nil {
				ans = line
			}
		} else {
			outf("%s", question)
		}
		ans = strings.ToLower(strings.TrimSpace(ans))
		if ans != "y" && ans != "yes" {
			outf("  · %s: skipped.\n", v)
			continue
		}
		val := vals[v]
		if val == "" {
			outf("  ✗ %s: no value in the env - skipped\n", v)
			continue
		}
		slug := providers.Slug(firstString(row.String("provider"), v))
		toolName, n := slug, 2
		for used[toolName] {
			if _, existing := tools[toolName]; f.replace && existing {
				break
			}
			toolName = fmt.Sprintf("%s-%d", slug, n)
			n++
		}
		if sid, ok := secrets[v]; f.replace && ok {
			if id, ok := tools[toolName]; ok && pyjson.Truthy(id) {
				if err := e.remove(c, idPath("/tools/", id)); err != nil {
					return err
				}
			}
			if err := e.remove(c, idPath("/secrets/", sid)); err != nil {
				return err
			}
		} else if ok {
			outf("  · %s: already registered (use --replace)\n", v)
			continue
		}
		rs, err := e.post(c, "/secrets", pyjson.Ordered("name", v, "value", val, "kind", "env"))
		if err != nil {
			return err
		}
		if rs.Status >= 400 {
			outf("  ✗ %s: secret failed (%d) %s\n", v, rs.Status, clip(string(rs.Body), 80))
			continue
		}
		sid := secretID(rs.Body)
		rt, err := e.post(c, "/tools", pyjson.Ordered("name", toolName, "base_url", base, "bindings", []any{withMember(binding, "secret_id", sid)}))
		if err != nil {
			return err
		}
		if rt.Status < 400 {
			used[toolName] = true
			outf("  ✓ %s %s\n", toolName, base)
		} else {
			outf("  ✗ %s: tool failed (%d) %s\n", v, rt.Status, clip(string(rt.Body), 80))
		}
	}
	return nil
}

func expandHome(p string) string {
	if p != "~" && !strings.HasPrefix(p, "~/") && !strings.HasPrefix(p, `~\`) {
		return p
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return p
	}
	return filepath.Join(home, filepath.FromSlash(p[1:]))
}

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
			loggedIn = loggedIn || agents.Exists(expandHome(p))
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
			return "no value for " + pyNone(s.envVar), nil
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

// pyTitle is Python's str.title().
func pyTitle(s string) string {
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
	entry := pyjson.Ordered("provider", pyTitle(name), "tokens", tokens, "base_url", baseURL,
		"auth", pyjson.Ordered("shape", "bearer"), "cli", cli)
	outln("\nShare this to add it to the catalog for everyone:\n  " + pyjson.Dumps(entry, pyjson.Options{Indent: -1, EnsureASCII: true}))
	return nil
}

func skillTag(kind string) string {
	switch kind {
	case "contract":
		return "tool (contract)"
	case "generated":
		return "tool (generated)"
	case "recipe_only":
		return "recipe-only"
	}
	return kind
}

var missingEnvRE = regexp.MustCompile(`^needs (?:env var|credential) (\S+)`)

// onlyResolvableGaps: the skill has no gap, or only missing credentials, which can be asked for.
func onlyResolvableGaps(d *skillscan.Detection) bool {
	for _, g := range d.Gaps {
		if !missingEnvRE.MatchString(g) {
			return false
		}
	}
	return true
}

func (e *Env) selectSkills(items []*skillscan.Detection, f uploadFlags) ([]*skillscan.Detection, error) {
	if f.selectNames != "" {
		want := selected(f.selectNames)
		var out []*skillscan.Detection
		for _, d := range items {
			if want[strings.ToLower(d.Name)] {
				out = append(out, d)
			}
		}
		return out, nil
	}
	if f.all || f.dryRun {
		return items, nil
	}
	if !prompt.StdinIsTerminal() {
		return nil, errNonInteractive
	}
	choices := make([]prompt.Choice, len(items))
	for i, d := range items {
		title := fmt.Sprintf("%-28s [%s] %s", d.Name, skillTag(d.Kind), d.BaseURL)
		if len(d.Gaps) > 0 {
			title += "  ⚠ " + d.Gaps[0]
		}
		choices[i] = prompt.Choice{Title: title, Checked: onlyResolvableGaps(d)}
	}
	picked, _ := prompt.Checkbox("Skills to import (tools + recipes):", choices)
	var out []*skillscan.Detection
	for _, i := range picked {
		out = append(out, items[i])
	}
	return out, nil
}

// askMissingCredentials asks once per variable for a credential neither the .env nor the machine
// environment had, and clears the gaps it satisfies.
func askMissingCredentials(chosen []*skillscan.Detection, values map[string]string) {
	var missing []string
	for _, d := range chosen {
		for _, g := range d.Gaps {
			if m := missingEnvRE.FindStringSubmatch(g); m != nil {
				if _, ok := values[m[1]]; !ok && !slices.Contains(missing, m[1]) {
					missing = append(missing, m[1])
				}
			}
		}
	}
	if len(missing) == 0 {
		return
	}
	outf("\n%d credential(s) your skills need aren't in the .env - enter to include those "+
		"skills, or leave blank to skip:\n", len(missing))
	for _, v := range missing {
		val, err := prompt.Hidden("  " + v + " (hidden): ")
		if err == nil && strings.TrimSpace(val) != "" {
			values[v] = strings.TrimSpace(val)
		}
	}
	for _, d := range chosen {
		var kept []string
		for _, g := range d.Gaps {
			if m := missingEnvRE.FindStringSubmatch(g); m != nil {
				if _, ok := values[m[1]]; ok {
					continue
				}
			}
			kept = append(kept, g)
		}
		d.Gaps = kept
	}
}

var credentialish = []string{"KEY", "TOKEN", "SECRET", "AUTH", "PAT", "PASSWORD", "CREDENTIAL", "APIKEY"}

func (e *Env) uploadSkills(f uploadFlags, skillsDir, envPath, cwd string) error {
	haveEnv := isRegularFile(envPath)
	envText := ""
	envNames := map[string]bool{}
	if haveEnv {
		envText = readEnvText(envPath)
		for _, n := range providers.VarNames(envText) {
			envNames[n] = true
		}
	}
	// A credential can also be in the machine environment; only credential-looking names count, so
	// a skill needing HOME or USER is not satisfied by an unrelated shell value.
	for _, kv := range os.Environ() {
		k, _, _ := strings.Cut(kv, "=")
		for _, a := range credentialish {
			if strings.Contains(strings.ToUpper(k), a) {
				envNames[k] = true
				break
			}
		}
	}
	catalog := e.uploadCatalog(f)
	stop := prompt.Spin("scanning 1 skill folder(s)")
	dets, err := skillscan.ScanSkills(skillsDir, catalog, envNames)
	stop()
	if err != nil {
		return output.Fail("%v", err)
	}
	seen := map[string]bool{}
	var tools, recipes []*skillscan.Detection
	blocked := 0
	for _, d := range dets {
		if seen[d.Name] {
			continue
		}
		seen[d.Name] = true
		switch d.Kind {
		case "contract", "generated":
			tools = append(tools, d)
			if len(d.Gaps) > 0 {
				blocked++
			}
		case "recipe_only":
			recipes = append(recipes, d)
		}
	}
	envNote := " · no .env found (env-credentialed skills will show a gap)"
	if haveEnv {
		rel, err := filepath.Rel(cwd, absFrom(cwd, envPath))
		if err != nil {
			rel = envPath
		}
		envNote = fmt.Sprintf(" · env: %s (%d vars)", rel, len(envNames))
	}
	outf("Scanned %s%s: %d API-tool skill(s) (%d with gaps), %d recipe-only.\n", skillsDir, envNote, len(tools), blocked, len(recipes))
	chosen, err := e.selectSkills(append(tools, recipes...), f)
	if err != nil {
		return err
	}
	if f.dryRun {
		if f.scan {
			outln("\nFound skills - `treg upload` imports these:")
		} else {
			outln("\nDRY RUN - would import:")
		}
		for _, d := range chosen {
			mark, gap := "✓", ""
			if len(d.Gaps) > 0 {
				mark, gap = "⚠", "  ⚠ "+strings.Join(d.Gaps, "; ")
			}
			outf("  %s %-28s %-18s %s%s\n", mark, d.Name, skillTag(d.Kind), d.BaseURL, gap)
		}
		return nil
	}
	if len(chosen) == 0 {
		outln("Nothing selected.")
		return nil
	}
	var toolSkills []*skillscan.Detection
	for _, d := range chosen {
		if d.Kind != "recipe_only" {
			toolSkills = append(toolSkills, d)
		}
	}
	need := skillscan.EnvNeeds(toolSkills)
	values := map[string]string{}
	if len(need) > 0 && haveEnv {
		values = providers.EnvValues(envText, need)
	}
	for _, k := range need {
		if v, ok := os.LookupEnv(k); ok {
			if _, have := values[k]; !have {
				values[k] = v
			}
		}
	}
	if prompt.StdinIsTerminal() {
		askMissingCredentials(chosen, values)
	}
	c := e.Client()
	stop = prompt.Spin("checking what's already registered")
	existingBundles := map[string][]any{}
	existingTools := map[string]bool{}
	rb, err := e.do(c, api.Request{Path: "/bundles"})
	if err == nil && rb.Status == 200 {
		var rows []map[string]any
		_ = json.Unmarshal(rb.Body, &rows)
		for _, b := range rows {
			existingBundles[str(b["name"])] = append(existingBundles[str(b["name"])], b["id"])
		}
	}
	if err == nil {
		var rt *api.Response
		rt, err = e.do(c, api.Request{Path: "/tools"})
		if err == nil && rt.Status == 200 {
			var rows []map[string]any
			_ = json.Unmarshal(rt.Body, &rows)
			for _, t := range rows {
				existingTools[str(t["name"])] = true
			}
		}
	}
	stop()
	if err != nil {
		return err
	}
	ok := 0
	for _, d := range chosen {
		var unmet []string
		if d.Kind != "recipe_only" {
			for _, k := range skillscan.EnvNeeds([]*skillscan.Detection{d}) {
				if _, have := values[k]; !have {
					unmet = append(unmet, k)
				}
			}
		}
		var other []string
		for _, g := range d.Gaps {
			if !strings.Contains(g, "needs credential") && !strings.Contains(g, "needs env var") {
				other = append(other, g)
			}
		}
		if len(unmet) > 0 || len(other) > 0 {
			reason := strings.Join(other, "; ")
			if reason == "" {
				reason = "missing " + strings.Join(unmet, ", ")
			}
			outf("  ⚠ %s: %s - skipped (fix + rerun)\n", d.Name, reason)
			continue
		}
		_, bundleExists := existingBundles[d.Name]
		if bundleExists || (d.Kind != "recipe_only" && existingTools[d.Name]) {
			if !f.replace {
				outf("  · %s: already registered (use --replace to update)\n", d.Name)
				outf("    ↗ %s\n", e.detailURL("skill", d.Name))
				continue
			}
			for _, id := range existingBundles[d.Name] {
				if err := e.remove(c, idPath("/bundles/", id)); err != nil {
					return err
				}
			}
		}
		payload, err := skillscan.BuildPayload(d, values)
		if err != nil {
			outf("  ✗ %s: %v\n", d.Name, err)
			continue
		}
		stop := prompt.Spin("uploading " + d.Name)
		r, err := e.post(c, "/skills", payload)
		stop()
		if err != nil {
			return err
		}
		if r.Status == 409 {
			outf("  · %s: a tool with this name already exists (use --replace)\n", d.Name)
			continue
		}
		if r.Status >= 400 {
			outf("  ✗ %s: %d %s\n", d.Name, r.Status, clip(string(r.Body), 100))
			continue
		}
		wrote, _ := skillscan.WriteContract(d)
		tag := "tool"
		if d.Kind == "recipe_only" {
			tag = "recipe"
		}
		note := ""
		if wrote != "" {
			note = "  [wrote treg.json]"
		}
		outf("  ✓ %-28s (%s)%s\n", d.Name, tag, note)
		outf("    ↗ %s\n", e.detailURL("skill", d.Name))
		ok++
	}
	outf("\nImported %d/%d skills. Share a skill by sending its ↗ link - "+
		"the page previews it and carries the agent install prompt.\n", ok, len(chosen))
	return nil
}
