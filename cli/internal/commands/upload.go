package commands

import (
	"bytes"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"

	"github.com/spf13/cobra"

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
		return fmt.Errorf("argument mode: invalid choice: %s (choose from 'env', 'skills', 'clis')", quote(args[0]))
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
	return e.do(c, api.Request{Method: "POST", Path: path, Body: []byte(pyjson.Dumps(body, pyjson.Body)), Header: jsonHeader})
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
