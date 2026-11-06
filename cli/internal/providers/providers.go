// Package providers classifies a .env's variables against the provider catalog: which provider a
// key belongs to, the auth shape its tool needs, which variables pair up, and which are the app's
// own secrets that must never become a tool. Detection reads variable names only; values are read
// only for the variables being registered.
package providers

import (
	_ "embed"
	"encoding/json"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"unicode"

	"github.com/superdesigndev/treg/cli/internal/pyjson"
)

// catalog.json is the Python CLI's providers.CATALOG at CatalogVersion, the floor when the
// registry's GET /providers.json is older or unreachable. tests/cli_go keeps the two equal.
//
//go:embed catalog.json
var bundled []byte

// Entry is one catalog provider, its members in catalog order.
type Entry = *pyjson.Object

// Bundled is the catalog this build carries and its version.
func Bundled() (int, []Entry) {
	v, err := pyjson.Decode(bundled)
	if err != nil {
		panic(err)
	}
	root := v.(*pyjson.Object)
	return Int(root, "version"), Entries(root)
}

// Entries is the providers list of a /providers.json body.
func Entries(root *pyjson.Object) []Entry {
	list, _ := root.Get("providers")
	items, _ := list.([]any)
	var out []Entry
	for _, item := range items {
		if e, ok := item.(*pyjson.Object); ok {
			out = append(out, e)
		}
	}
	return out
}

// Int is a member read as Python's int(x or 0) reads it; ok is false where that would raise.
func IntOK(o *pyjson.Object, key string) (int, bool) {
	v, _ := o.Get(key)
	switch t := v.(type) {
	case nil:
		return 0, true
	case bool:
		if t {
			return 1, true
		}
		return 0, true
	case json.Number:
		if n, err := strconv.Atoi(string(t)); err == nil {
			return n, true
		}
		if f, err := t.Float64(); err == nil {
			return int(f), true
		}
	case string:
		if t == "" {
			return 0, true
		}
		if n, err := strconv.Atoi(strings.TrimSpace(t)); err == nil {
			return n, true
		}
	}
	return 0, false
}

func Int(o *pyjson.Object, key string) int {
	n, _ := IntOK(o, key)
	return n
}

// Obj is a member that is an object, or nil.
func Obj(o *pyjson.Object, key string) *pyjson.Object {
	if o == nil {
		return nil
	}
	v, _ := o.Get(key)
	obj, _ := v.(*pyjson.Object)
	return obj
}

// Strings is a member that is a list, its string items.
func Strings(o *pyjson.Object, key string) []string {
	if o == nil {
		return nil
	}
	v, _ := o.Get(key)
	items, _ := v.([]any)
	var out []string
	for _, item := range items {
		if s, ok := item.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

// Truthy is Python's bool() of a decoded member.
func Truthy(o *pyjson.Object, key string) bool {
	if o == nil {
		return false
	}
	v, _ := o.Get(key)
	return truthy(v)
}

func truthy(v any) bool {
	switch t := v.(type) {
	case nil:
		return false
	case bool:
		return t
	case string:
		return t != ""
	case json.Number:
		f, _ := t.Float64()
		return f != 0
	case []any:
		return len(t) > 0
	case *pyjson.Object:
		return t.Len() > 0
	}
	return true
}

// Strip is Python's str.strip().
func Strip(s string) string { return strings.TrimFunc(s, IsSpace) }

// IsSpace is Python's str.isspace for one character.
func IsSpace(r rune) bool {
	switch r {
	case 0x1c, 0x1d, 0x1e, 0x1f:
		return true
	}
	return unicode.IsSpace(r)
}

func lines(text string) []string {
	text = strings.ReplaceAll(strings.ReplaceAll(text, "\r\n", "\n"), "\r", "\n")
	return strings.Split(text, "\n")
}

func envKey(line string) (string, string, bool) {
	line = Strip(line)
	if line == "" || strings.HasPrefix(line, "#") || !strings.Contains(line, "=") {
		return "", "", false
	}
	key, val, _ := strings.Cut(line, "=")
	key = Strip(key)
	if strings.HasPrefix(strings.ToLower(key), "export ") {
		key = Strip(key[len("export "):])
	}
	return key, val, true
}

// VarNames is every variable a dotenv text names, in order. Values are discarded.
func VarNames(text string) []string {
	var names []string
	for _, l := range lines(text) {
		if key, _, ok := envKey(l); ok && key != "" {
			names = append(names, key)
		}
	}
	return names
}

// EnvValues is the values of the named variables, one surrounding quote pair stripped.
func EnvValues(text string, names []string) map[string]string {
	want := map[string]bool{}
	for _, n := range names {
		want[n] = true
	}
	out := map[string]string{}
	for _, l := range lines(text) {
		key, val, ok := envKey(l)
		if !ok || !want[key] {
			continue
		}
		v := Strip(val)
		if r := []rune(v); len(r) >= 2 && r[0] == r[len(r)-1] && (r[0] == '"' || r[0] == '\'') {
			v = string(r[1 : len(r)-1])
		}
		out[key] = v
	}
	return out
}

// MatchProvider is the provider whose longest token is one of the name's components.
func MatchProvider(comps map[string]bool, catalog []Entry) Entry {
	var best Entry
	bestLen := -1
	for _, p := range catalog {
		for _, tok := range Strings(p, "tokens") {
			if comps[tok] && len(tok) > bestLen {
				best, bestLen = p, len(tok)
			}
		}
	}
	return best
}

func Components(name string) map[string]bool {
	comps := map[string]bool{}
	for _, c := range strings.Split(strings.ToUpper(name), "_") {
		comps[c] = true
	}
	return comps
}

var nonAlnum = regexp.MustCompile(`[^a-z0-9]+`)

func norm(s string) string {
	return strings.Trim(nonAlnum.ReplaceAllString(strings.ToLower(s), "-"), "-")
}

// MatchSkill matches a skill folder name to a provider by a `skills` alias or the provider's name.
func MatchSkill(name string, catalog []Entry) Entry {
	n := norm(name)
	if n == "" {
		return nil
	}
	for _, p := range catalog {
		for _, a := range Strings(p, "skills") {
			if norm(a) == n {
				return p
			}
		}
		if norm(p.String("provider")) == n {
			return p
		}
	}
	return nil
}

// CLIEnvVar is the variable a CLI reads its credential from: the first env inject's name, or an
// argv inject's env_from.
func CLIEnvVar(cli *pyjson.Object) string {
	v, _ := cli.Get("inject")
	items, _ := v.([]any)
	for _, item := range items {
		e, ok := item.(*pyjson.Object)
		if !ok {
			continue
		}
		via := "env"
		if raw, ok := e.Get("via"); ok {
			via, _ = raw.(string)
		}
		if via == "env" && Truthy(e, "name") {
			return e.String("name")
		}
		if via == "argv" && Truthy(e, "env_from") {
			return e.String("env_from")
		}
	}
	return ""
}

// Decision is what auto-import does with one catalog CLI. An empty field is an absent key.
type Decision struct {
	Status, Tier, Env, Login, Action, Reason string
}

// ClassifyCLI decides from machine facts the caller gathered: is the bin installed, is its
// credential variable set, is a login config present.
func ClassifyCLI(entry Entry, installed, secretPresent, loggedIn bool) Decision {
	cli := Obj(entry, "cli")
	if cli == nil {
		cli = pyjson.NewObject()
	}
	mech := "env"
	if v, ok := cli.Get("auth_mechanism"); ok {
		mech, _ = v.(string)
	}
	if Truthy(cli, "unsupported") || mech == "device" {
		reason := "not automatable"
		if Truthy(cli, "reason") {
			reason = cli.String("reason")
		}
		return Decision{Status: "unsupported", Reason: reason}
	}
	if !installed {
		return Decision{Status: "not_installed", Action: cli.String("install")}
	}
	login := cli.String("login_cmd")
	if login == "" {
		bin := cli.String("bin")
		if _, ok := cli.Get("bin"); !ok {
			bin = "None"
		}
		login = bin + " login"
	}
	if mech == "env" || mech == "argv" {
		if secretPresent {
			return Decision{Status: "ready", Tier: "server"}
		}
		if loggedIn {
			return Decision{Status: "ready", Tier: "local"}
		}
		d := Decision{Status: "needs_key", Env: CLIEnvVar(cli)}
		if Truthy(cli, "detect") {
			d.Login = login
		}
		return d
	}
	if loggedIn {
		return Decision{Status: "ready", Tier: "local"}
	}
	return Decision{Status: "needs_login", Action: "run `" + login + "`"}
}

// Detection is one classified credential or credential pair.
type Detection struct {
	Kind            string // matched | oauth_pair | basic_pair | app_internal | unknown_secret | config
	Vars            []string
	Provider        string
	BaseURL         string
	Auth            *pyjson.Object
	Probe           string
	RequiredHeaders *pyjson.Object
	Note            string
}

var pairForms = []struct {
	form  string
	parts [2]string
}{
	{"oauth2", [2]string{"CLIENT_ID", "CLIENT_SECRET"}},
	{"basic", [2]string{"ACCOUNT_SID", "AUTH_TOKEN"}},
}

var appInternal = []string{
	"SECRET_KEY", "SESSION_SECRET", "JWT_SECRET", "ENCRYPTION_KEY", "FERNET", "SIGNING_KEY",
	"COOKIE_SECRET", "CSRF", "APP_SECRET", "APP_KEY", "DATABASE_URL", "DB_URL", "DB_PASSWORD",
	"ADMIN_TOKEN", "ADMIN_PASSWORD", "SALT", "WEBHOOK_SECRET",
}

var secretHints = set("KEY", "TOKEN", "SECRET", "PASSWORD", "PWD", "APIKEY", "CREDENTIAL", "CREDENTIALS",
	"AUTH", "ACCESS", "PRIVATE", "BEARER")

var configHints = set("URL", "URI", "HOST", "PORT", "FROM", "MODE", "ENV", "REGION", "DEBUG", "LEVEL",
	"PATH", "DIR", "NAME", "ID", "VERSION", "PUBLIC", "TIMEOUT", "ENABLED", "DISABLE",
	"PROJECT", "DOMAIN", "ENDPOINT", "PREFIX", "BUCKET", "ORG", "MODEL", "BASE",
	"ORGANIZATION", "HOME", "DSN")

var alwaysInternal = []string{"WEBHOOK_SECRET", "SIGNING_KEY", "SIGNING_SECRET", "JWT_SECRET", "ENCRYPTION_KEY",
	"COOKIE_SECRET", "SESSION_SECRET"}

func set(items ...string) map[string]bool {
	m := map[string]bool{}
	for _, i := range items {
		m[i] = true
	}
	return m
}

func intersects(a, b map[string]bool) bool {
	for k := range a {
		if b[k] {
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

type pairForm struct {
	form, part string
}

func pairFormOf(upper string) *pairForm {
	for _, pf := range pairForms {
		for _, part := range pf.parts {
			if strings.Contains(upper, part) {
				return &pairForm{pf.form, part}
			}
		}
	}
	return nil
}

func pairPrefix(name, part string) string {
	return strings.Trim(strings.ReplaceAll(strings.ToUpper(name), part, ""), "_")
}

func authShape(p Entry) string {
	return Obj(p, "auth").String("shape")
}

// ScanEnv classifies every variable a dotenv text names.
func ScanEnv(text string, catalog []Entry) []Detection {
	type row struct {
		name     string
		provider Entry
		pf       *pairForm
	}
	var raw []row
	for _, name := range VarNames(text) {
		upper := strings.ToUpper(name)
		provider := MatchProvider(Components(name), catalog)
		pf := pairFormOf(upper)
		if pf != nil && pf.form == "basic" && (provider == nil || authShape(provider) != "basic") {
			pf = nil
		}
		raw = append(raw, row{name, provider, pf})
	}
	var dets []Detection
	used := map[int]bool{}
	for i, r := range raw {
		if used[i] || r.pf == nil || r.provider == nil {
			continue
		}
		prefix := pairPrefix(r.name, r.pf.part)
		mates := []int{i}
		for j, o := range raw {
			if j != i && !used[j] && o.provider != nil && o.provider.String("provider") == r.provider.String("provider") &&
				o.pf != nil && o.pf.form == r.pf.form && o.pf.part != r.pf.part && pairPrefix(o.name, o.pf.part) == prefix {
				mates = append(mates, j)
			}
		}
		if len(mates) > 2 {
			mates = mates[:2]
		}
		if len(mates) < 2 {
			continue
		}
		var vars []string
		for _, j := range mates {
			used[j] = true
			vars = append(vars, raw[j].name)
		}
		auth := pyjson.Ordered("shape", r.pf.form)
		kind, note := "basic_pair", "id + secret → Basic auth"
		if r.pf.form == "oauth2" {
			kind, note = "oauth_pair", "OAuth app pair → connect flow, not a Bearer key"
			if oauth := Obj(r.provider, "oauth"); oauth != nil && oauth.Len() > 0 {
				for _, k := range oauth.Keys() {
					v, _ := oauth.Get(k)
					auth.Set(k, v)
				}
			}
		}
		dets = append(dets, Detection{Kind: kind, Vars: vars, Provider: r.provider.String("provider"),
			BaseURL: r.provider.String("base_url"), Auth: auth, Note: note})
	}
	for i, r := range raw {
		if used[i] {
			continue
		}
		upper := strings.ToUpper(r.name)
		comps := Components(r.name)
		switch {
		case r.provider != nil:
			name := r.provider.String("provider")
			switch {
			case r.pf != nil:
				kind := "basic_pair"
				if r.pf.form == "oauth2" {
					kind = "oauth_pair"
				}
				dets = append(dets, Detection{Kind: kind, Vars: []string{r.name}, Provider: name,
					BaseURL: r.provider.String("base_url"), Auth: pyjson.Ordered("shape", r.pf.form),
					Note: "incomplete pair - only the " + r.pf.part + " half found"})
			case containsAny(upper, alwaysInternal):
				dets = append(dets, Detection{Kind: "app_internal", Vars: []string{r.name},
					Note: name + " signing/app secret - not a callable key"})
			case intersects(comps, configHints) && !intersects(comps, secretHints):
				dets = append(dets, Detection{Kind: "config", Vars: []string{r.name},
					Note: name + " config (not a credential)"})
			default:
				headers := pyjson.NewObject()
				if h := Obj(r.provider, "required_headers"); h != nil {
					headers = h.Copy()
				}
				auth := Obj(r.provider, "auth")
				if auth == nil {
					auth = pyjson.NewObject()
				}
				dets = append(dets, Detection{Kind: "matched", Vars: []string{r.name}, Provider: name,
					BaseURL: r.provider.String("base_url"), Auth: auth.Copy(), Probe: r.provider.String("probe"),
					RequiredHeaders: headers})
			}
		case containsAny(upper, appInternal):
			dets = append(dets, Detection{Kind: "app_internal", Vars: []string{r.name}, Note: "app's own secret - never a tool"})
		case intersects(comps, secretHints):
			dets = append(dets, Detection{Kind: "unknown_secret", Vars: []string{r.name}, Note: "looks like a credential; no provider match"})
		default:
			dets = append(dets, Detection{Kind: "config", Vars: []string{r.name}})
		}
	}
	return dets
}

func Slug(provider string) string {
	return strings.ReplaceAll(strings.ToLower(provider), " ", "-")
}

// BuildBinding turns an auth shape into a tool binding, or nil when it has none (oauth2, a query
// credential without a parameter name).
func BuildBinding(auth *pyjson.Object) *pyjson.Object {
	if auth == nil {
		return nil
	}
	switch auth.String("shape") {
	case "bearer":
		return pyjson.Ordered("injector", "env", "location", "header", "name", "Authorization", "format", "Bearer {secret}")
	case "api_key_header":
		header, fmtv := "Authorization", "{secret}"
		if v, ok := auth.Get("header"); ok {
			header, _ = v.(string)
		}
		if v, ok := auth.Get("format"); ok {
			fmtv, _ = v.(string)
		}
		return pyjson.Ordered("injector", "env", "location", "header", "name", header, "format", fmtv)
	case "query":
		param := auth.String("param")
		if param == "" {
			return nil
		}
		return pyjson.Ordered("injector", "env", "location", "query", "name", param, "format", "{secret}")
	case "basic":
		return pyjson.Ordered("injector", "env", "location", "header", "name", "Authorization", "format", "Basic {secret}")
	}
	return nil
}

func firstContaining(names []string, subs ...string) string {
	for _, v := range names {
		if containsAny(strings.ToUpper(v), subs) {
			return v
		}
	}
	return ""
}

// OAuthParts is the client-id and client-secret variables of an OAuth pair.
func OAuthParts(names []string) (string, string) {
	return firstContaining(names, "CLIENT_ID"), firstContaining(names, "CLIENT_SECRET")
}

// OAuthReady is a complete OAuth pair whose provider has connect-flow endpoints.
func OAuthReady(d Detection) bool {
	cid, csec := OAuthParts(d.Vars)
	return cid != "" && csec != "" && d.Auth != nil && Truthy(d.Auth, "auth_uri") && Truthy(d.Auth, "token_uri")
}

// BasicParts is the username and password variables of a Basic pair.
func BasicParts(names []string) (string, string) {
	user := firstContaining(names, "SID", "ACCOUNT", "USER")
	for _, v := range names {
		if v != user && containsAny(strings.ToUpper(v), []string{"TOKEN", "SECRET", "PASSWORD", "KEY"}) {
			return user, v
		}
	}
	return user, ""
}

// Action is a planned registration, or why one detection cannot be registered automatically.
type Action struct {
	Detection       Detection
	Supported       bool
	SecretName      string
	ToolName        string
	BaseURL         string
	Binding         *pyjson.Object
	RequiredHeaders *pyjson.Object
	Combine         []string
	Health          *pyjson.Object
	Reason          string
}

// PlanActions is an action for every offerable detection; app secrets and config are never offered.
func PlanActions(dets []Detection) []*Action {
	var actions []*Action
	for _, d := range dets {
		switch d.Kind {
		case "matched":
			binding := BuildBinding(d.Auth)
			a := &Action{Detection: d, Supported: binding != nil, SecretName: d.Vars[0],
				ToolName: Slug(firstNonEmpty(d.Provider, d.Vars[0])), BaseURL: d.BaseURL, Binding: binding,
				RequiredHeaders: d.RequiredHeaders.Copy()}
			if d.Probe != "" {
				a.Health = pyjson.Ordered("path", d.Probe, "expect_status", 200)
			}
			if binding == nil {
				shape := "?"
				if d.Auth.Len() > 0 {
					shape = "None"
					if v, ok := d.Auth.Get("shape"); ok && v != nil {
						shape, _ = v.(string)
					}
				}
				a.Reason = "auth shape " + shape + " not auto-registered yet"
			}
			actions = append(actions, a)
		case "oauth_pair":
			actions = append(actions, &Action{Detection: d, BaseURL: d.BaseURL, ToolName: Slug(d.Provider),
				Reason: "OAuth - use `treg oauth connect` (Phase 2.5)"})
		case "basic_pair":
			user, pass := BasicParts(d.Vars)
			if user != "" && pass != "" {
				actions = append(actions, &Action{Detection: d, Supported: true, BaseURL: d.BaseURL, ToolName: Slug(d.Provider),
					SecretName: strings.ToUpper(strings.ReplaceAll(Slug(d.Provider), "-", "_")) + "_BASIC",
					Binding:    BuildBinding(pyjson.Ordered("shape", "basic")), Combine: []string{user, pass}})
			} else {
				actions = append(actions, &Action{Detection: d, BaseURL: d.BaseURL, ToolName: Slug(d.Provider), Reason: "incomplete Basic pair"})
			}
		case "unknown_secret":
			actions = append(actions, &Action{Detection: d, SecretName: d.Vars[0],
				Reason: "no provider match - resolve with `treg upload --llm`, or register by hand: " +
					"`treg secret add <name> --env-var <VAR>` + `treg tool add <name> --base-url …`"})
		}
	}
	used := map[string]bool{}
	for _, a := range actions {
		if !a.Supported || a.ToolName == "" {
			continue
		}
		base, n := a.ToolName, 2
		for used[a.ToolName] {
			a.ToolName = base + "-" + strconv.Itoa(n)
			n++
		}
		used[a.ToolName] = true
	}
	return actions
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if v != "" {
			return v
		}
	}
	return ""
}

const llmSystem = "You identify which third-party API an environment variable authenticates, from its NAME only. " +
	"For each name that is clearly a real, public API provider's credential, return the provider, its " +
	"REST API base_url (https, the API root - no trailing resource path), and the auth shape. SKIP " +
	"anything that is an app's own secret (SECRET_KEY, SESSION_SECRET, DATABASE_URL…) or that you can't " +
	"confidently map to a specific provider."

// LLMPrompt is the system and user messages asking a model to map unknown names to providers.
func LLMPrompt(names []string) (string, string) {
	ask := `Map these env var names to API providers. Return ONLY JSON: {"resolved":[{"var":"NAME",` +
		`"provider":"Name","base_url":"https://...","auth":{"shape":"bearer|api_key_header|query",` +
		`"header":"X-Api-Key","param":"api_key"}}]}. Include "header" only for api_key_header, "param" ` +
		"only for query. Omit any name you are unsure about.\nNames: " + strings.Join(names, ", ")
	return llmSystem, ask
}

// LLMParse keeps the model's well-formed suggestions: a var, a safe https base_url and a known auth
// shape, each var once. Reasoning a model writes inline before `</think>` is dropped first.
func LLMParse(text string) []*pyjson.Object {
	if i := strings.LastIndex(text, "</think>"); i >= 0 {
		text = text[i+len("</think>"):]
	}
	data, err := pyjson.Decode([]byte(text))
	if err != nil {
		i, j := strings.Index(text, "{"), strings.LastIndex(text, "}")
		if i < 0 || j <= i {
			return nil
		}
		if data, err = pyjson.Decode([]byte(text[i : j+1])); err != nil {
			return nil
		}
	}
	root, ok := data.(*pyjson.Object)
	if !ok {
		return nil
	}
	v, _ := root.Get("resolved")
	items, _ := v.([]any)
	var out []*pyjson.Object
	seen := map[string]bool{}
	for _, item := range items {
		e, ok := item.(*pyjson.Object)
		if !ok {
			continue
		}
		auth := Obj(e, "auth")
		shape := auth.String("shape")
		v := e.String("var")
		base := e.String("base_url")
		if v == "" || base == "" || (shape != "bearer" && shape != "api_key_header" && shape != "query") {
			continue
		}
		if seen[v] || !SafeBaseURL(base) {
			continue
		}
		seen[v] = true
		out = append(out, e)
	}
	return out
}

// SafeBaseURL is an https URL on a public host: not loopback, private or link-local.
func SafeBaseURL(raw string) bool {
	u, err := url.Parse(raw)
	if err != nil {
		return false
	}
	host := strings.ToLower(u.Hostname())
	if strings.ToLower(u.Scheme) != "https" || host == "" || !strings.Contains(host, ".") {
		return false
	}
	if host == "localhost" || strings.HasSuffix(host, ".local") || strings.HasSuffix(host, ".internal") || strings.HasSuffix(host, ".localhost") {
		return false
	}
	for _, p := range []string{"127.", "10.", "192.168.", "169.254.", "0."} {
		if strings.HasPrefix(host, p) {
			return false
		}
	}
	if host == "::1" {
		return false
	}
	if strings.HasPrefix(host, "172.") {
		parts := strings.Split(host, ".")
		if len(parts) > 1 {
			if n, err := strconv.Atoi(parts[1]); err == nil && n >= 16 && n <= 31 {
				return false
			}
		}
	}
	return true
}
