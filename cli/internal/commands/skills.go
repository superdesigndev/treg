package commands

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"maps"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/agents"
	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/buildinfo"
	"github.com/superdesigndev/treg/cli/internal/config"
	"github.com/superdesigndev/treg/cli/internal/output"
	"github.com/superdesigndev/treg/cli/internal/prompt"
	"github.com/superdesigndev/treg/cli/internal/pyjson"
)

// missingSubcommand: a command group alone is a usage error, as in the Python CLI.
func missingSubcommand(cmd *cobra.Command, _ []string) error {
	var names []string
	for _, c := range cmd.Commands() {
		if !c.Hidden && c.Name() != "help" {
			names = append(names, c.Name())
		}
	}
	return fmt.Errorf("the following arguments are required: <subcommand> (%s)", strings.Join(names, ", "))
}

func skillCmd(env *Env) *cobra.Command {
	skill := &cobra.Command{
		Use:   "skill",
		Short: "Register / manage skills (a recipe + its secrets + tool(s), as one bundle).",
		Example: "  treg skill ls\n" +
			"  treg skill install seo-blog-writer\n" +
			"  treg skill bootstrap --skills all",
		Args: cobra.NoArgs,
		RunE: missingSubcommand,
	}
	ls := &cobra.Command{
		Use:     "ls",
		Short:   "List registered skills (bundles).",
		Example: "  treg skill ls",
		Args:    cobra.NoArgs,
		RunE: func(*cobra.Command, []string) error {
			r, err := env.do(env.Client(), api.Request{Path: "/bundles"})
			if err != nil {
				return err
			}
			return env.Out.Show(r)
		},
	}
	skill.AddCommand(ls, skillInstallCmd(env), skillBootstrapCmd(env))
	return skill
}

type installFlags struct {
	all, allAgents, global, force bool
	dir, agent                    string
}

func skillInstallCmd(env *Env) *cobra.Command {
	var f installFlags
	cmd := &cobra.Command{
		Use:   "install [name]",
		Short: "Pull a shared skill into every agent's skills dir (.agents/skills + .claude/skills).",
		Example: "  treg skill install seo-blog-writer\n" +
			"  treg skill install --all\n" +
			"  treg skill install foo --agent cursor",
		Args: cobra.MaximumNArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			name := ""
			if len(args) == 1 {
				name = args[0]
			}
			return env.skillInstall(name, f)
		},
	}
	fl := cmd.Flags()
	fl.BoolVar(&f.all, "all", false, "install every skill in the org")
	fl.StringVar(&f.dir, "dir", "", "pin one explicit target directory (skips agent fan-out)")
	fl.StringVar(&f.agent, "agent", "", "install for one agent only ("+strings.Join(agents.Names(), ", ")+")")
	fl.BoolVar(&f.allAgents, "all-agents", false, "fan out to every known agent's dir, not just the default two")
	fl.BoolVar(&f.global, "global", false, "write into detected-installed agents' GLOBAL dirs (not the project)")
	fl.BoolVar(&f.force, "force", false, "overwrite an existing local SKILL.md")
	return cmd
}

type bundleRow struct {
	ID   any
	Name string
}

func (e *Env) skillInstall(name string, f installFlags) error {
	bases, ok := agents.Targets(f.dir, f.agent, f.global, f.allAgents)
	if !ok {
		return output.Fail("unknown agent %s - one of: %s", quote(f.agent), strings.Join(agents.Names(), ", "))
	}
	c := e.Client()
	stop := prompt.Spin("fetching the skill list")
	r, err := e.do(c, api.Request{Path: "/bundles"})
	stop()
	if err != nil {
		return err
	}
	if r.Status >= 400 {
		return e.Out.Show(r)
	}
	var raw []map[string]any
	_ = json.Unmarshal(r.Body, &raw)
	var targets []bundleRow
	for _, b := range raw {
		row := bundleRow{ID: b["id"], Name: str(b["name"])}
		if f.all || row.Name == name {
			targets = append(targets, row)
		}
	}
	switch {
	case f.all:
	case name != "" && len(targets) == 0:
		return e.installPublicSkill(name, bases, f.force)
	case name == "":
		return output.Fail("give a skill name or --all")
	}
	p := e.Colors()
	seen := map[string]bool{}
	var keptExisting []string
	n := 0
	for _, b := range targets {
		if seen[b.Name] {
			continue
		}
		seen[b.Name] = true
		if b.Name == "" || strings.ContainsAny(b.Name, `/\`) || b.Name == ".." || b.Name == "." {
			outf("  ✗ %s: unsafe skill name - skipped\n", quote(b.Name))
			continue
		}
		stop := prompt.Spin("downloading " + b.Name)
		d, err := e.do(c, api.Request{Path: "/bundles/" + str(b.ID)})
		stop()
		if err != nil {
			return err
		}
		if d.Status >= 400 {
			outf("  ✗ %s: %d\n", b.Name, d.Status)
			continue
		}
		parsed, _ := pyjson.Decode(d.Body)
		bundle, _ := parsed.(*pyjson.Object)
		recipe := bundle.String("recipe")
		if strings.TrimSpace(recipe) == "" {
			outf("  · %s: no recipe - skipped\n", b.Name)
			continue
		}
		files, _ := bundle.Get("files")
		var wroteTo []string
		extra := 0
		for _, base := range bases {
			dest := filepath.Join(base, b.Name)
			skillMD := filepath.Join(dest, "SKILL.md")
			if agents.Exists(skillMD) && !f.force {
				continue
			}
			if err := os.MkdirAll(dest, 0o777); err != nil {
				return output.Fail("treg: %v", err)
			}
			if err := os.WriteFile(skillMD, []byte(recipe), 0o666); err != nil {
				return output.Fail("treg: %v", err)
			}
			extra = writeBundleFiles(dest, files)
			wroteTo = append(wroteTo, base)
		}
		var toolNames []string
		toolsV, _ := bundle.Get("tools")
		for _, t := range pyjson.List(toolsV) {
			if obj, ok := t.(*pyjson.Object); ok {
				toolNames = append(toolNames, obj.String("name"))
			}
		}
		tail := ""
		if extra > 0 {
			tail += fmt.Sprintf("  +%d file(s)", extra)
		}
		if len(toolNames) > 0 {
			tail += "  (tools: " + strings.Join(toolNames, ", ") + " - call via `treg call`)"
		}
		if len(wroteTo) > 0 {
			outf("  ✓ %-28s → %s%s\n", b.Name, strings.Join(wroteTo, ", "), tail)
			n++
		} else {
			outf("  · %-28s already on disk - kept your copy\n", b.Name)
			keptExisting = append(keptExisting, b.Name)
		}
	}
	outf("\nInstalled %d skill(s) into %s\n", n, strings.Join(bases, ", "))
	if len(keptExisting) > 0 {
		outf("\n%s⚠ %d skill(s) already existed locally and were kept (not overwritten):%s %s\n",
			p.Amber, len(keptExisting), p.Reset, strings.Join(keptExisting, ", "))
		outf("  To replace one with the team's version:  %streg skill install <name> --force%s\n", p.Bold, p.Reset)
		outf("  %sOverwrites local edits - confirm before you --force.%s\n", p.Muted, p.Reset)
	}
	return nil
}

// installPublicSkill: a name that is not one of the team's skills but is one of the registry's
// public skills installs from the registry's skill index, so `treg skill install lead-signals`
// works as `treg skill bootstrap --skills lead-signals` does, into the dirs `skill install` targets.
func (e *Env) installPublicSkill(name string, bases []string, force bool) error {
	base := strings.TrimRight(e.Registry(), "/")
	notFound := output.Fail("no skill named %s in this org (see `treg skill ls`)", quote(name))
	optional := optionalSkills(base)
	listed := false
	for _, o := range optional {
		listed = listed || o.name == name
	}
	if !listed {
		return notFound
	}
	text, ok := publicSkillText(base, name)
	if !ok {
		return output.Fail("could not download the %s skill from %s; rerun the same command to retry", name, base)
	}
	ledger := loadSkillLedger()
	var wroteTo []string
	for _, b := range bases {
		path := filepath.Join(b, name, "SKILL.md")
		if agents.Exists(path) && !force {
			continue
		}
		if err := writeSkill(path, text, ledger); err != nil {
			saveSkillLedger(ledger)
			return output.Fail("treg: %v", err)
		}
		wroteTo = append(wroteTo, b)
	}
	saveSkillLedger(ledger)
	n := 0
	if len(wroteTo) > 0 {
		outf("  ✓ %-28s → %s  (a public skill of this registry)\n", name, strings.Join(wroteTo, ", "))
		n = 1
	} else {
		outf("  · %-28s already on disk - kept your copy\n", name)
	}
	outf("\nInstalled %d skill(s) into %s\n", n, strings.Join(bases, ", "))
	return nil
}

// writeBundleFiles rebuilds a skill's companion files under dest. Each must stay inside dest: a
// bundle cannot write anywhere else, whatever its paths say.
func writeBundleFiles(dest string, files any) int {
	obj, ok := files.(*pyjson.Object)
	if !ok {
		return 0
	}
	root := agents.Resolve(dest)
	written := 0
	for _, rel := range obj.Keys() {
		v, _ := obj.Get(rel)
		content, isText := v.(string)
		rel = strings.ReplaceAll(rel, `\`, "/")
		if rel == "" || strings.HasPrefix(rel, "/") || slices.Contains(strings.Split(rel, "/"), "..") || rel == "SKILL.md" || !isText {
			continue
		}
		target := agents.Resolve(filepath.Join(root, filepath.FromSlash(rel)))
		inside := target == root || strings.HasPrefix(target, root+string(filepath.Separator))
		if !inside || filepath.VolumeName(filepath.FromSlash(rel)) != "" {
			outf("    ✗ unsafe path skipped: %s\n", rel)
			continue
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o777); err != nil {
			continue
		}
		if err := os.WriteFile(target, []byte(content), 0o666); err != nil {
			continue
		}
		written++
	}
	return written
}

func skillBootstrapCmd(env *Env) *cobra.Command {
	var allAgents, project bool
	var skills string
	cmd := &cobra.Command{
		Use:   "bootstrap",
		Short: "Install the official treg skill into every detected agent (used by the installer).",
		Example: "  treg skill bootstrap\n" +
			"  treg skill bootstrap --skills lead-signals,recruit\n" +
			"  treg skill bootstrap --skills all",
		Args: cobra.NoArgs,
		RunE: func(*cobra.Command, []string) error {
			return env.skillBootstrap(allAgents, project, skills)
		},
	}
	fl := cmd.Flags()
	fl.BoolVar(&allAgents, "all-agents", false, "every known agent's dir, not just the ones detected on this machine")
	fl.BoolVar(&project, "project", false, "write into project dirs instead of the per-user global dirs")
	fl.StringVar(&skills, "skills", "", "also install the registry's other skills: comma-separated names, or `all`")
	return cmd
}

var publicHTTP = &http.Client{Timeout: 15 * time.Second}

// fetchRegistryPage is a GET of a public registry page, redirects followed.
func fetchRegistryPage(rawURL string, header [][2]string) (int, []byte, error) {
	req, err := http.NewRequest(http.MethodGet, rawURL, nil)
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("User-Agent", buildinfo.UserAgent())
	for _, kv := range header {
		req.Header.Set(kv[0], kv[1])
	}
	resp, err := publicHTTP.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	return resp.StatusCode, body, err
}

type optionalSkill struct {
	name, summary string
}

var skillSlug = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,63}$`)

// optionalSkills is the registry's other public skills from the well-known index `npx skills add`
// reads, or nil when the index cannot be read.
func optionalSkills(base string) []optionalSkill {
	_, body, err := fetchRegistryPage(base+"/.well-known/skills/index.json", nil)
	if err != nil {
		return nil
	}
	v, err := pyjson.Decode(body)
	if err != nil {
		return nil
	}
	idx, ok := v.(*pyjson.Object)
	if !ok {
		return nil
	}
	out := []optionalSkill{}
	skillsV, _ := idx.Get("skills")
	for _, item := range pyjson.List(skillsV) {
		entry, ok := item.(*pyjson.Object)
		if !ok {
			continue
		}
		nameV, _ := entry.Get("name")
		name := pyjson.Text(nameV)
		if name == "treg" || !skillSlug.MatchString(name) {
			continue
		}
		var blurb string
		if sv, _ := entry.Get("summary"); pyjson.Truthy(sv) {
			blurb = pyjson.Text(sv)
		} else {
			description := ""
			if dv, ok := entry.Get("description"); ok {
				description = pyjson.Text(dv)
			}
			blurb = shortenSummary(description, 100)
		}
		out = append(out, optionalSkill{name, strings.Join(strings.Fields(blurb), " ")})
	}
	return out
}

// shortenSummary is textwrap.shortenSummary(text, width, placeholder="…").
func shortenSummary(text string, width int) string {
	words := strings.Fields(text)
	joined := strings.Join(words, " ")
	if utf8.RuneCountInString(joined) <= width {
		return joined
	}
	line := ""
	for _, w := range words {
		next := w
		if line != "" {
			next = line + " " + w
		}
		if utf8.RuneCountInString(next)+1 > width {
			break
		}
		line = next
	}
	return line + "…"
}

func publicSkillText(base, name string) (string, bool) {
	status, body, err := fetchRegistryPage(base+"/.well-known/skills/"+name+"/SKILL.md", nil)
	if err != nil || status != 200 || !strings.HasPrefix(string(body), "---") {
		return "", false
	}
	return string(body), true
}

func skillLedgerPath() string { return config.Sibling("skills.json") }

// loadSkillLedger is SKILL.md path -> sha256 of the text treg last wrote there.
func loadSkillLedger() map[string]string {
	ledger := map[string]string{}
	raw, err := os.ReadFile(skillLedgerPath())
	if err != nil {
		return ledger
	}
	v, err := pyjson.Decode(raw)
	if err != nil {
		return ledger
	}
	obj, ok := v.(*pyjson.Object)
	if !ok {
		return ledger
	}
	for _, k := range obj.Keys() {
		val, _ := obj.Get(k)
		ledger[k] = pyjson.Text(val)
	}
	return ledger
}

func saveSkillLedger(ledger map[string]string) {
	path := skillLedgerPath()
	if err := agents.MkdirLeaf(filepath.Dir(path), 0o700); err != nil {
		return
	}
	obj := pyjson.NewObject()
	for _, k := range slices.Sorted(maps.Keys(ledger)) {
		obj.Set(k, ledger[k])
	}
	text := pyjson.Dumps(obj, pyjson.Options{Indent: 2, EnsureASCII: true, SortKeys: true}) + "\n"
	_ = os.WriteFile(path, []byte(text), 0o666)
}

func skillSHA(text string) string {
	sum := sha256.Sum256([]byte(text))
	return hex.EncodeToString(sum[:])
}

// skillIsOurs: bootstrap may write this SKILL.md when it is missing, or is the copy bootstrap wrote
// and nobody has edited since. A copy from before the ledger counts only when it is a lone SKILL.md
// that names this skill and links this registry.
func skillIsOurs(path, name, base string, ledger map[string]string) bool {
	if !agents.Exists(path) {
		return true
	}
	raw, err := os.ReadFile(path)
	if err != nil || !utf8.Valid(raw) {
		return false
	}
	entries, err := os.ReadDir(filepath.Dir(path))
	if err != nil {
		return false
	}
	text := agents.UniversalNewlines(string(raw))
	if sha, ok := ledger[agents.Resolve(path)]; ok {
		return sha == skillSHA(text)
	}
	lone := len(entries) == 1 && entries[0].Name() == "SKILL.md"
	namesIt := regexp.MustCompile(`(?m)^name:\s*` + regexp.QuoteMeta(name) + `\s*$`).MatchString(text)
	return lone && namesIt && strings.Contains(text, base+"/")
}

func writeSkill(path, text string, ledger map[string]string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o777); err != nil {
		return err
	}
	if err := os.WriteFile(path, []byte(text), 0o666); err != nil {
		return err
	}
	ledger[agents.Resolve(path)] = skillSHA(text)
	return nil
}

// skillBootstrap installs the treg skill into every detected agent, and the registry's other
// skills only when --skills names them; a copy treg already wrote is refreshed in place. A SKILL.md
// treg did not write, or one edited since, is never overwritten.
func (e *Env) skillBootstrap(allAgents, project bool, skillsArg string) error {
	base := strings.TrimRight(e.Registry(), "/")
	say := func(format string, a ...any) {
		if !e.JSON {
			outf(format, a...)
		}
	}
	// Signed in, the registry answers for the team: a section for a feature limited to some teams
	// reaches only their members.
	var header [][2]string
	if token := api.Token(e.Cfg); token != "" {
		header = append(header, [2]string{"X-Treg-Token", token})
		if org := e.EffectiveOrg(); org != "" {
			header = append(header, [2]string{"X-Treg-Org", org})
		}
	}
	status, body, err := fetchRegistryPage(base+"/skill.md", header)
	if err == nil && status >= 400 {
		err = fmt.Errorf("HTTP %d", status)
	}
	if err != nil {
		return output.Fail("could not fetch the treg skill from %s: %v", base, err)
	}
	recipe := string(body)
	if strings.TrimSpace(recipe) == "" {
		return output.Fail("the server returned an empty skill")
	}
	bases, _ := agents.Targets("", "", !project, allAgents)
	ledger := loadSkillLedger()
	var skipped []string
	n := 0
	for _, b := range bases {
		dest := filepath.Join(b, "treg", "SKILL.md")
		if !skillIsOurs(dest, "treg", base, ledger) {
			skipped = append(skipped, dest)
			continue
		}
		if err := writeSkill(dest, recipe, ledger); err != nil {
			saveSkillLedger(ledger)
			return output.Fail("treg: %v", err)
		}
		say("  ✓ treg → %s\n", dest)
		n++
		// The skill once installed as "tools-registry"; a leftover copy would load as a duplicate.
		legacy := filepath.Join(b, "tools-registry")
		if entries, err := os.ReadDir(legacy); err == nil && len(entries) == 1 && entries[0].Name() == "SKILL.md" {
			if os.Remove(filepath.Join(legacy, "SKILL.md")) == nil && os.Remove(legacy) == nil {
				say("    (removed the old tools-registry skill folder, renamed to treg)\n")
			}
		}
	}

	optional := optionalSkills(base)
	names := map[string]bool{}
	for _, o := range optional {
		names[o.name] = true
	}
	asked := map[string]bool{}
	for s := range strings.SplitSeq(skillsArg, ",") {
		if s = strings.ToLower(strings.TrimSpace(s)); s != "" {
			asked[s] = true
		}
	}
	if asked["all"] {
		delete(asked, "all")
		for k := range names {
			asked[k] = true
		}
	}
	unknown, unread := []string{}, []string{}
	if optional != nil {
		for k := range asked {
			if !names[k] {
				unknown = append(unknown, k)
			}
		}
		slices.Sort(unknown)
	} else {
		unread = slices.Sorted(maps.Keys(asked))
	}
	installed, failed := []string{}, []string{}
	for _, o := range optional {
		var owned, writeTo []string
		for _, b := range bases {
			p := filepath.Join(b, o.name, "SKILL.md")
			if skillIsOurs(p, o.name, base, ledger) {
				owned = append(owned, p)
			} else if asked[o.name] {
				skipped = append(skipped, p)
			}
		}
		if asked[o.name] {
			writeTo = owned
		} else {
			for _, p := range owned {
				if agents.Exists(p) {
					writeTo = append(writeTo, p)
				}
			}
		}
		if len(writeTo) == 0 {
			continue
		}
		text, ok := publicSkillText(base, o.name)
		if !ok {
			if asked[o.name] {
				failed = append(failed, o.name)
			}
			continue
		}
		for _, p := range writeTo {
			if err := writeSkill(p, text, ledger); err != nil {
				saveSkillLedger(ledger)
				return output.Fail("treg: %v", err)
			}
		}
		installed = append(installed, o.name)
		say("  ✓ %s → %d location(s)\n", o.name, len(writeTo))
	}
	saveSkillLedger(ledger)
	var available []optionalSkill
	for _, o := range optional {
		if !slices.Contains(installed, o.name) && !slices.Contains(failed, o.name) {
			available = append(available, o)
		}
	}

	if e.JSON {
		done := installed
		if n > 0 {
			done = append([]string{"treg"}, installed...)
		}
		avail := []any{}
		for _, o := range available {
			avail = append(avail, pyjson.Ordered("name", o.name, "summary", o.summary))
		}
		body := pyjson.Ordered("installed", done, "locations", n, "unknown", unknown, "failed", failed,
			"skipped", append([]string{}, skipped...), "available", avail)
		if optional == nil {
			body.Set("error", "could not read the skill list from "+base)
		}
		outln(pyjson.Dumps(body, pyjson.Pretty))
		return nil
	}
	detected := agents.Detected()
	tail := "no agents detected, used sensible defaults"
	if len(detected) > 0 {
		tail = "detected: " + strings.Join(detected, ", ")
	}
	done := installed
	if n > 0 {
		done = append([]string{"treg"}, installed...)
	}
	what := strings.Join(done, ", ")
	if what == "" {
		what = "nothing"
	}
	outf("\nInstalled %s into %d location(s)  (%s).\n", what, n, tail)
	for _, p := range skipped {
		outln("Left alone, not written by treg: " + p)
	}
	if len(unread) > 0 {
		outf("Could not read the skill list from %s, so these were not installed: %s\n", base, strings.Join(unread, ", "))
		outln("Rerun the same command to retry.")
	}
	if len(unknown) > 0 {
		outln("Not a skill on this registry: " + strings.Join(unknown, ", "))
	}
	if len(failed) > 0 {
		outf("Could not download: %s. Rerun the same command to retry.\n", strings.Join(failed, ", "))
	}
	if len(available) > 0 {
		width := 0
		for _, o := range available {
			width = max(width, utf8.RuneCountInString(o.name))
		}
		outln("\nOptional skills from this registry (not installed):")
		for _, o := range available {
			outf("  %-*s  %s\n", width, o.name, o.summary)
		}
		outln("If you are an agent: tell the user in one sentence each what these do and ask which they")
		outln("want; then run")
		outln("  treg skill bootstrap --skills <name>,<name>")
		outln("  treg skill bootstrap --skills all")
	}
	return nil
}
