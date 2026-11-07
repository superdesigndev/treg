package commands

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/output"
	"github.com/superdesigndev/treg/cli/internal/prompt"
	"github.com/superdesigndev/treg/cli/internal/providers"
	"github.com/superdesigndev/treg/cli/internal/skillscan"
)

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
