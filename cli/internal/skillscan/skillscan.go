// Package skillscan classifies a directory of skills for `treg upload skills`: a skill that calls
// an authenticated API becomes a tool with its secret and recipe (from its treg.json contract, or
// one generated from its scripts and secret files), a knowledge skill becomes a recipe-only bundle.
// Detection reads scripts, SKILL.md and secret file names; values are read only when uploading.
package skillscan

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/superdesigndev/treg/cli/internal/providers"
	"github.com/superdesigndev/treg/cli/internal/pyjson"
)

const ContractFile = "treg.json"

var (
	baseRE = regexp.MustCompile(`(?i)(?:API|BASE|BASE_URL|API_URL|ENDPOINT|HOST)\s*=\s*["'](https?://[^"']+)["']`)
	envRE  = regexp.MustCompile(`os\.environ(?:\.get)?\(\s*["']([A-Z][A-Z0-9_]+)["']|\$\{?([A-Z][A-Z0-9_]+)\}?`)
	urlRE  = regexp.MustCompile(`https?://[a-zA-Z0-9.\-]+(?:/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=\-]*)?`)

	appConfigRE = regexp.MustCompile(`(?i)client[_-]?secret|(^|[_-])credentials\.json$`)
	stemSepRE   = regexp.MustCompile(`[_\s]+`)
)

var (
	authy         = []string{"KEY", "TOKEN", "SECRET", "AUTH", "PAT"}
	scriptGlobs   = []string{".py", ".sh", ".js", ".ts", ".mjs"}
	secretDirs    = []string{".secret", ".secrets"}
	recipeFiles   = []string{"SKILL.md", "skill.md", "README.md"}
	docHostHints  = []string{"developer", "docs.", "api-docs.", "reference", "github.com", "readme.io"}
	skipDirs      = map[string]bool{".secret": true, ".secrets": true, ".git": true, "__pycache__": true, "node_modules": true, ".venv": true, ".idea": true}
	skipNames     = map[string]bool{"SKILL.md": true, ContractFile: true, ".DS_Store": true}
	maxFileBytes  = 512 * 1024
	maxTotalBytes = 4 * 1024 * 1024
)

// Detection is one skill folder's classification.
type Detection struct {
	Name, Path string
	Kind       string // contract | generated | recipe_only | skip
	BaseURL    string
	Secrets    []*pyjson.Object // each {source: file|env, file?|env?, name, kind}
	Bindings   []any
	Health     any
	Examples   []any
	Gaps       []string
	Note       string
	CLI        *pyjson.Object
}

func exists(p string) bool {
	_, err := os.Stat(p)
	return err == nil
}

func isDir(p string) bool {
	info, err := os.Stat(p)
	return err == nil && info.IsDir()
}

func isFile(p string) bool {
	info, err := os.Stat(p)
	return err == nil && info.Mode().IsRegular()
}

// IsSkillDir is a folder with a SKILL.md.
func IsSkillDir(p string) bool {
	return exists(filepath.Join(p, "SKILL.md")) || exists(filepath.Join(p, "skill.md"))
}

// Name is a path's last component as Python's Path.name spells it.
func Name(p string) string {
	clean := filepath.Clean(p)
	if clean == "." {
		return ""
	}
	return filepath.Base(clean)
}

// ReadText is Python's read_text(errors="replace"): invalid UTF-8 replaced, universal newlines.
func ReadText(p string) (string, error) {
	raw, err := os.ReadFile(p)
	if err != nil {
		return "", err
	}
	return newlines(strings.ToValidUTF8(string(raw), "�")), nil
}

func newlines(s string) string {
	if !strings.Contains(s, "\r") {
		return s
	}
	return strings.ReplaceAll(strings.ReplaceAll(s, "\r\n", "\n"), "\r", "\n")
}

func lessName(a, b string) bool {
	if runtime.GOOS == "windows" {
		return strings.ToLower(a) < strings.ToLower(b)
	}
	return a < b
}

// sortedEntries is a directory's entries in the order Python sorts Path objects.
func sortedEntries(dir string) []os.DirEntry {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	sort.SliceStable(entries, func(i, j int) bool { return lessName(entries[i].Name(), entries[j].Name()) })
	return entries
}

// lessParts orders relative paths as Python orders Path objects: part by part.
func lessParts(a, b string) bool {
	pa := strings.Split(filepath.ToSlash(a), "/")
	pb := strings.Split(filepath.ToSlash(b), "/")
	for i := 0; i < len(pa) && i < len(pb); i++ {
		if pa[i] != pb[i] {
			return lessName(pa[i], pb[i])
		}
	}
	return len(pa) < len(pb)
}

// walk lists every path under root as rglob("*") does: hidden entries included, symlinked
// directories not entered. Paths are relative to root, in Path order.
func walk(root string) []string {
	var out []string
	_ = filepath.WalkDir(root, func(p string, d os.DirEntry, err error) error {
		if err != nil {
			if d != nil && d.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		if p == root {
			return nil
		}
		rel, _ := filepath.Rel(root, p)
		out = append(out, rel)
		return nil
	})
	sort.SliceStable(out, func(i, j int) bool { return lessParts(out[i], out[j]) })
	return out
}

func readScripts(dir string) string {
	all := walk(dir)
	var b strings.Builder
	for _, ext := range scriptGlobs {
		for _, rel := range all {
			name := filepath.Base(rel)
			if !matchExt(name, ext) {
				continue
			}
			text, err := ReadText(filepath.Join(dir, rel))
			if err != nil {
				continue
			}
			b.WriteString(text)
			b.WriteString("\n")
		}
	}
	return b.String()
}

func matchExt(name, ext string) bool {
	if runtime.GOOS == "windows" {
		return strings.HasSuffix(strings.ToLower(name), ext)
	}
	return strings.HasSuffix(name, ext)
}

// ScanSkills classifies every skill under dir: each sub-folder with a SKILL.md (dot-folders
// skipped), or dir itself when it is one skill.
func ScanSkills(dir string, catalog []providers.Entry, envNames map[string]bool) ([]*Detection, error) {
	if !isDir(dir) {
		return nil, fmt.Errorf("%s is not a directory", dir)
	}
	var subdirs []string
	for _, e := range sortedEntries(dir) {
		p := filepath.Join(dir, e.Name())
		if isDir(p) && !strings.HasPrefix(e.Name(), ".") && IsSkillDir(p) {
			subdirs = append(subdirs, p)
		}
	}
	if len(subdirs) == 0 && IsSkillDir(dir) {
		return []*Detection{classify(dir, catalog, envNames)}, nil
	}
	var out []*Detection
	for _, d := range subdirs {
		out = append(out, classify(d, catalog, envNames))
	}
	return out, nil
}

// LoadContract is the skill's treg.json, nil when there is none; an error when it is not valid
// JSON or its cli block would be refused at registration.
func LoadContract(dir string) (*pyjson.Object, error) {
	f := filepath.Join(dir, ContractFile)
	if !exists(f) {
		return nil, nil
	}
	text, err := ReadText(f)
	if err != nil {
		return nil, err
	}
	v, err := pyjson.Decode([]byte(text))
	if err != nil {
		return nil, fmt.Errorf("%s is not valid JSON: %v", ContractFile, err)
	}
	contract, ok := v.(*pyjson.Object)
	if !ok {
		return nil, fmt.Errorf("%s is not a JSON object", ContractFile)
	}
	if cli, ok := contract.Get("cli"); ok && cli != nil && !validCLIProfile(cli) {
		return nil, fmt.Errorf("%s cli block is not valid", ContractFile)
	}
	return contract, nil
}

func contractCLI(contract *pyjson.Object) *pyjson.Object {
	cli := providers.Obj(contract, "cli")
	if cli == nil {
		return nil
	}
	out := cli.Copy()
	if _, ok := out.Get("enabled"); !ok {
		out.Set("enabled", true)
	}
	return out
}

func list(v any) []any {
	items, _ := v.([]any)
	return items
}

func classify(d string, catalog []providers.Entry, envNames map[string]bool) *Detection {
	name := Name(d)
	contract, err := LoadContract(d)
	if err != nil {
		return &Detection{Name: name, Path: d, Kind: "skip", Note: "bad treg.json: " + err.Error()}
	}
	if contract.Len() > 0 && providers.Truthy(contract, "base_url") {
		det := &Detection{Name: name, Path: d, Kind: "contract", BaseURL: contract.String("base_url"), Note: "has treg.json",
			CLI: contractCLI(contract)}
		secretsV, _ := contract.Get("secrets")
		for _, item := range list(secretsV) {
			s, ok := item.(*pyjson.Object)
			if !ok {
				continue
			}
			src := pyjson.Ordered("source", "file")
			for _, k := range s.Keys() {
				v, _ := s.Get(k)
				src.Set(k, v)
			}
			det.Secrets = append(det.Secrets, src)
			switch {
			case providers.Truthy(s, "file") && !exists(ResolveSecretPath(d, s.String("file"))):
				det.Gaps = append(det.Gaps, "treg.json secret file missing: "+s.String("file"))
			case providers.Truthy(s, "env") && !envNames[s.String("env")]:
				det.Gaps = append(det.Gaps, "needs env var "+s.String("env")+" - not found in the env")
			}
		}
		bindingsV, _ := contract.Get("bindings")
		det.Bindings = list(bindingsV)
		det.Health, _ = contract.Get("health")
		examplesV, _ := contract.Get("examples")
		det.Examples = list(examplesV)
		if !providers.Truthy(contract, "bindings") && !providers.Truthy(contract, "cli") {
			det.Gaps = append(det.Gaps, "treg.json has no bindings (tool would have no auth)")
		}
		flagHeaderCollisions(det)
		return det
	}

	scripts := readScripts(d)
	base := baseRE.FindStringSubmatch(scripts)
	files := SecretFiles(d)
	var found []string
	for _, m := range envRE.FindAllStringSubmatch(scripts, -1) {
		for _, g := range m[1:] {
			if g != "" {
				found = append(found, g)
			}
		}
	}
	var authEnvs []string
	seenEnv := map[string]bool{}
	for _, e := range found {
		if seenEnv[e] {
			continue
		}
		seenEnv[e] = true
		for _, part := range strings.Split(e, "_") {
			if contains(authy, part) {
				authEnvs = append(authEnvs, e)
				break
			}
		}
	}

	if base == nil && len(files) == 0 && len(authEnvs) == 0 {
		if p := providers.MatchSkill(name, catalog); p != nil {
			if cli := providers.Obj(p, "cli"); cli.Len() > 0 && !providers.Truthy(cli, "unsupported") {
				return catalogCLIDetection(name, d, p, envNames)
			}
		}
		return &Detection{Name: name, Path: d, Kind: "recipe_only", Note: "knowledge skill - recipe only"}
	}

	det := &Detection{Name: name, Path: d, Kind: "generated"}
	baseURL := ""
	if base != nil {
		baseURL = base[1]
		if containsAny(strings.ToLower(baseURL), docHostHints) {
			baseURL = ""
		}
	}
	if baseURL == "" {
		baseURL = guessBaseURL(d)
	}
	if p := providers.MatchSkill(name, catalog); p != nil {
		baseURL = p.String("base_url")
	}
	switch {
	case len(files) > 0:
		secrets, bindings := AutoBindings(d, files)
		for _, s := range secrets {
			src := pyjson.Ordered("source", "file")
			for _, k := range s.Keys() {
				v, _ := s.Get(k)
				src.Set(k, v)
			}
			det.Secrets = append(det.Secrets, src)
		}
		det.Bindings = bindings
		if baseURL == "" && len(authEnvs) > 0 {
			if p := providers.MatchProvider(providers.Components(authEnvs[0]), catalog); p != nil {
				baseURL = p.String("base_url")
			}
		}
	case len(authEnvs) > 0:
		v := authEnvs[0]
		p := providers.MatchProvider(providers.Components(v), catalog)
		if p != nil && baseURL == "" {
			baseURL = p.String("base_url")
		}
		var binding *pyjson.Object
		if p != nil {
			binding = providers.BuildBinding(providers.Obj(p, "auth"))
		}
		if binding == nil {
			binding = pyjson.Ordered("injector", "env", "location", "header", "name", "Authorization", "format", "Bearer {secret}")
		}
		det.Secrets = append(det.Secrets, pyjson.Ordered("source", "env", "env", v, "name", name, "kind", "env"))
		b := pyjson.Ordered("secret", name)
		for _, k := range binding.Keys() {
			bv, _ := binding.Get(k)
			b.Set(k, bv)
		}
		det.Bindings = append(det.Bindings, b)
		if !envNames[v] {
			det.Gaps = append(det.Gaps, "needs env var "+v+" - not found in the env")
		}
		if len(authEnvs) > 1 {
			det.Note = fmt.Sprintf("reads %d auth vars (%s); only %s bound - verify", len(authEnvs), strings.Join(authEnvs, ", "), v)
		}
	}
	det.BaseURL = baseURL
	if baseURL == "" {
		det.Gaps = append(det.Gaps, "base_url not found - set it manually or use --llm")
	}
	flagHeaderCollisions(det)
	return det
}

func contains(items []string, s string) bool {
	for _, i := range items {
		if i == s {
			return true
		}
	}
	return false
}

func containsAny(s string, subs []string) bool {
	for _, sub := range subs {
		if strings.Contains(s, sub) {
			return true
		}
	}
	return false
}

// catalogCLIDetection: a secret-less skill the catalog knows as a CLI becomes a runnable cli tool,
// each env credential sourced from the machine at upload.
func catalogCLIDetection(name, d string, entry providers.Entry, envNames map[string]bool) *Detection {
	src := providers.Obj(entry, "cli")
	cli := pyjson.NewObject()
	for _, k := range src.Keys() {
		if k != "verified" {
			v, _ := src.Get(k)
			cli.Set(k, v)
		}
	}
	cli.Set("enabled", true)
	det := &Detection{Name: name, Path: d, Kind: "generated", BaseURL: entry.String("base_url"), Note: "catalog CLI skill"}
	injectV, _ := cli.Get("inject")
	var inject []any
	seen := map[string]bool{}
	for _, item := range list(injectV) {
		e, ok := item.(*pyjson.Object)
		if !ok {
			inject = append(inject, item)
			continue
		}
		e = e.Copy()
		inject = append(inject, e)
		via := "env"
		if v, ok := e.Get("via"); ok {
			via, _ = v.(string)
		}
		envVar := e.String("env_from")
		if via == "env" {
			envVar = e.String("name")
		}
		if envVar == "" {
			continue
		}
		local := strings.ReplaceAll(strings.ToLower(envVar), "_", "-")
		for seen[local] {
			local += "-x"
		}
		seen[local] = true
		e.Set("secret", local)
		det.Secrets = append(det.Secrets, pyjson.Ordered("source", "env", "env", envVar, "name", local, "kind", "env"))
		if !envNames[envVar] {
			det.Gaps = append(det.Gaps, "needs credential "+envVar+" (not found in your environment)")
		}
	}
	if inject == nil {
		inject = []any{}
	}
	cli.Set("inject", inject)
	det.Bindings = []any{}
	det.CLI = cli
	return det
}

func flagHeaderCollisions(det *Detection) {
	count := map[string]int{}
	var order []string
	for _, item := range det.Bindings {
		b, ok := item.(*pyjson.Object)
		if !ok {
			continue
		}
		loc := "header"
		if v, ok := b.Get("location"); ok {
			loc, _ = v.(string)
		}
		if loc != "header" {
			continue
		}
		h := "Authorization"
		if providers.Truthy(b, "name") {
			h = b.String("name")
		}
		h = strings.ToLower(h)
		if count[h] == 0 {
			order = append(order, h)
		}
		count[h]++
	}
	var dup []string
	for _, h := range order {
		if count[h] > 1 {
			dup = append(dup, h)
		}
	}
	sort.Strings(dup)
	if len(dup) > 0 {
		det.Gaps = append(det.Gaps, "ships multiple credentials that all map to one header ("+strings.Join(dup, ", ")+") - add a "+
			"treg.json mapping each credential to its own header (e.g. Authorization + developer-token)")
	}
}

// ResolveSecretPath finds a contract's secret file, tolerating .secret for .secrets and back.
func ResolveSecretPath(base, rel string) string {
	exact := filepath.Join(base, rel)
	if exists(exact) {
		return exact
	}
	parts := strings.Split(filepath.ToSlash(filepath.Clean(rel)), "/")
	if len(parts) > 0 && contains(secretDirs, parts[0]) {
		for _, sd := range secretDirs {
			alt := filepath.Join(append([]string{base, sd}, parts[1:]...)...)
			if exists(alt) {
				return alt
			}
		}
	}
	return exact
}

// SecretFiles is the files in the skill's .secret and .secrets folders.
func SecretFiles(dir string) []string {
	var out []string
	for _, sd := range secretDirs {
		p := filepath.Join(dir, sd)
		if !isDir(p) {
			continue
		}
		for _, e := range sortedEntries(p) {
			if f := filepath.Join(p, e.Name()); isFile(f) {
				out = append(out, f)
			}
		}
	}
	return out
}

func readRecipe(dir string) string {
	for _, name := range recipeFiles {
		if f := filepath.Join(dir, name); exists(f) {
			text, _ := ReadText(f)
			return text
		}
	}
	return ""
}

// guessBaseURL is the best API host named in SKILL.md and the top-level scripts, preferring api.*.
func guessBaseURL(dir string) string {
	text := readRecipe(dir)
	var pys []string
	for _, e := range sortedEntries(dir) {
		if matchExt(e.Name(), ".py") {
			pys = append(pys, e.Name())
		}
	}
	for _, name := range pys {
		t, err := ReadText(filepath.Join(dir, name))
		if err != nil {
			continue
		}
		text += "\n" + t
	}
	var hosts []string
	for _, m := range urlRE.FindAllString(text, -1) {
		scheme, rest, _ := strings.Cut(m, "://")
		host, _, _ := strings.Cut(rest, "/")
		host = strings.ToLower(host)
		if host == "" || containsAny(host, docHostHints) {
			continue
		}
		base := scheme + "://" + host
		if !contains(hosts, base) {
			hosts = append(hosts, base)
		}
	}
	if len(hosts) == 0 {
		return ""
	}
	sort.SliceStable(hosts, func(i, j int) bool {
		return strings.Contains(hosts[i], "//api.") && !strings.Contains(hosts[j], "//api.")
	})
	return hosts[0]
}

// guessKind: env (plain text) | secret_file (JSON) | oauth (JSON with a refresh_token).
func guessKind(name, text string) string {
	if !strings.HasSuffix(name, ".json") {
		return "env"
	}
	v, err := pyjson.Decode([]byte(text))
	if err != nil {
		return "secret_file"
	}
	if o, ok := v.(*pyjson.Object); ok {
		if _, ok := o.Get("refresh_token"); ok {
			return "oauth"
		}
	}
	return "secret_file"
}

func oauthSecretField(f string) string {
	text, err := ReadText(f)
	if err != nil {
		return "access_token"
	}
	v, err := pyjson.Decode([]byte(text))
	if err != nil {
		return "access_token"
	}
	if o, ok := v.(*pyjson.Object); ok {
		_, hasAccess := o.Get("access_token")
		_, hasToken := o.Get("token")
		if !hasAccess && hasToken {
			return "token"
		}
	}
	return "access_token"
}

// stem is Python's Path.stem: the name without its last suffix.
func stem(name string) string {
	i := strings.LastIndex(name, ".")
	if 0 < i && i < len(name)-1 {
		return name[:i]
	}
	return name
}

func headerFromStem(s string) string {
	h := strings.ToLower(strings.Trim(stemSepRE.ReplaceAllString(s, "-"), "-"))
	if h == "" {
		return "authorization"
	}
	return h
}

// AutoBindings gives each credential file a secret and its own header: the primary token (the
// OAuth one, else the first) on `Authorization: Bearer`, every other on a header named from its
// file. OAuth app config mints tokens and is never sent, so it is skipped.
func AutoBindings(base string, files []string) ([]*pyjson.Object, []any) {
	var creds []string
	for _, f := range files {
		if !appConfigRE.MatchString(filepath.Base(f)) {
			creds = append(creds, f)
		}
	}
	if len(creds) == 0 {
		return nil, nil
	}
	kinds := map[string]string{}
	for _, f := range creds {
		text, _ := ReadText(f)
		kinds[f] = guessKind(filepath.Base(f), text)
	}
	primary := creds[0]
	for _, f := range creds {
		if kinds[f] == "oauth" {
			primary = f
			break
		}
	}
	single := len(creds) == 1
	var secrets []*pyjson.Object
	var bindings []any
	seen := map[string]bool{}
	for _, f := range creds {
		kind := kinds[f]
		name := Name(base)
		if !single {
			name = stem(filepath.Base(f))
		}
		for seen[name] {
			name = name + "-" + strconv.Itoa(len(seen))
		}
		seen[name] = true
		rel, _ := filepath.Rel(base, f)
		secrets = append(secrets, pyjson.Ordered("file", rel, "name", name, "kind", kind))
		if f == primary {
			field := "access_token"
			if kind == "oauth" {
				field = oauthSecretField(f)
			}
			bindings = append(bindings, pyjson.Ordered("secret", name, "injector", kind, "location", "header",
				"name", "Authorization", "format", "Bearer {secret}", "secret_field", field))
		} else {
			bindings = append(bindings, pyjson.Ordered("secret", name, "injector", kind, "location", "header",
				"name", headerFromStem(stem(filepath.Base(f))), "format", "{secret}", "secret_field", "access_token"))
		}
	}
	return secrets, bindings
}

// ToContract is the treg.json for a tool skill, without the internal source marker.
func ToContract(det *Detection) *pyjson.Object {
	var secrets []any
	for _, s := range det.Secrets {
		c := s.Copy()
		c.Delete("source")
		secrets = append(secrets, c)
	}
	c := pyjson.Ordered("name", det.Name, "base_url", det.BaseURL, "secrets", orEmpty(secrets),
		"bindings", orEmpty(det.Bindings), "examples", orEmpty(det.Examples))
	if truthy(det.Health) {
		c.Set("health", det.Health)
	}
	if det.CLI.Len() > 0 {
		c.Set("cli", det.CLI)
	}
	return c
}

func orEmpty(items []any) []any {
	if items == nil {
		return []any{}
	}
	return items
}

func truthy(v any) bool {
	o := pyjson.Ordered("v", v)
	return providers.Truthy(o, "v")
}

// WriteContract saves a generated skill's treg.json beside it, never over an existing one.
func WriteContract(det *Detection) (string, error) {
	if det.Kind != "generated" {
		return "", nil
	}
	path := filepath.Join(det.Path, ContractFile)
	if exists(path) {
		return "", nil
	}
	text := pyjson.Dumps(ToContract(det), pyjson.Options{Indent: 2, EnsureASCII: true})
	if err := os.WriteFile(path, []byte(text), 0o666); err != nil {
		return "", err
	}
	return path, nil
}

// EnvNeeds is the variables the skills' env secrets read, each once.
func EnvNeeds(dets []*Detection) []string {
	var out []string
	seen := map[string]bool{}
	for _, d := range dets {
		for _, s := range d.Secrets {
			if e := s.String("env"); e != "" && !seen[e] {
				seen[e] = true
				out = append(out, e)
			}
		}
	}
	return out
}

// CollectFiles is every text file of the skill folder by relative path, minus the recipe, the
// contract, secrets, junk and binaries, within the size limits.
func CollectFiles(dir string) *pyjson.Object {
	out := pyjson.NewObject()
	total := 0
	for _, rel := range walk(dir) {
		p := filepath.Join(dir, rel)
		info, err := os.Stat(p)
		if err != nil || !info.Mode().IsRegular() {
			continue
		}
		parts := strings.Split(filepath.ToSlash(rel), "/")
		skip := false
		for _, part := range parts {
			if skipDirs[part] {
				skip = true
			}
		}
		name := filepath.Base(rel)
		if skip || skipNames[name] || strings.HasSuffix(name, ".pyc") || info.Size() > int64(maxFileBytes) {
			continue
		}
		raw, err := os.ReadFile(p)
		if err != nil || !utf8.Valid(raw) {
			continue
		}
		text := newlines(string(raw))
		total += len(text)
		if total > maxTotalBytes {
			break
		}
		out.Set(filepath.ToSlash(rel), text)
	}
	return out
}

// BuildPayload is the POST /skills body. File secrets are read from disk and env secrets from
// values; a recipe-only skill ships its recipe and files alone.
func BuildPayload(det *Detection, values map[string]string) (*pyjson.Object, error) {
	recipe := readRecipe(det.Path)
	files := CollectFiles(det.Path)
	if det.Kind == "recipe_only" {
		return pyjson.Ordered("name", det.Name, "recipe", recipe, "files", files, "secrets", []any{}, "tools", []any{}), nil
	}
	secrets := []any{}
	for _, s := range det.Secrets {
		var val string
		switch {
		case providers.Truthy(s, "file"):
			text, err := os.ReadFile(ResolveSecretPath(det.Path, s.String("file")))
			if err != nil {
				return nil, err
			}
			val = providers.Strip(newlines(string(text)))
		case providers.Truthy(s, "env"):
			v, ok := values[s.String("env")]
			if !ok {
				return nil, errors.New("missing env value for " + s.String("env"))
			}
			val = v
		default:
			return nil, fmt.Errorf("secret '%s' has neither a file nor an env source", s.String("name"))
		}
		kind := "env"
		if v, ok := s.Get("kind"); ok {
			kind, _ = v.(string)
		}
		secrets = append(secrets, pyjson.Ordered("local_name", s.String("name"), "value", val, "kind", kind))
	}
	tool := pyjson.Ordered("name", det.Name, "base_url", det.BaseURL, "bindings", orEmpty(det.Bindings))
	if truthy(det.Health) {
		tool.Set("health_check", det.Health)
	}
	if len(det.Examples) > 0 {
		tool.Set("examples", det.Examples)
	}
	if det.CLI.Len() > 0 {
		tool.Set("cli", det.CLI)
	}
	return pyjson.Ordered("name", det.Name, "recipe", recipe, "files", files, "secrets", secrets, "tools", []any{tool}), nil
}
