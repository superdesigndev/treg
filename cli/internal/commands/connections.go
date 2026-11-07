package commands

import (
	"errors"
	"fmt"
	"io"
	"os"
	"regexp"
	"strings"
	"time"

	"github.com/spf13/cobra"
	"github.com/spf13/pflag"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/output"
	"github.com/superdesigndev/treg/cli/internal/prompt"
	"github.com/superdesigndev/treg/cli/internal/providers"
	"github.com/superdesigndev/treg/cli/internal/pyjson"
	"github.com/superdesigndev/treg/cli/internal/term"
)

func connectionsCmd(env *Env) *cobra.Command {
	cmd := leaf("connections", "Your connected accounts: connect providers, health, expiry.",
		"treg connections                               # same as `connections ls`",
		"treg connections connect --provider google-search-console",
		"treg connections resources 12",
		"treg connections use 12 sc-domain:example.com", "treg connections rm 12")
	cmd.Args = cobra.ArbitraryArgs
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		if len(args) > 0 {
			return invalidSubcommand(cmd, args[0])
		}
		env.Note("command", "connections_ls")
		return env.show(api.Request{Path: "/connections"})
	}
	ls := leaf("ls", "List connections with health + expiry.", "treg connections ls")
	ls.Args = cobra.NoArgs
	ls.RunE = func(*cobra.Command, []string) error {
		return env.show(api.Request{Path: "/connections"})
	}
	resources := leaf("resources <id>", "What this connection can act on (sites/properties/accounts).",
		"treg connections resources 12")
	resources.Args = cobra.ExactArgs(1)
	resources.RunE = func(_ *cobra.Command, args []string) error {
		id, err := intArg("id", args[0])
		if err != nil {
			return err
		}
		return env.show(api.Request{Path: "/connections/" + string(id) + "/resources"})
	}
	use := leaf("use <id> <resource>", "Select which resource this connection acts on.",
		"treg connections use 12 sc-domain:example.com")
	use.Args = cobra.ExactArgs(2)
	use.RunE = func(_ *cobra.Command, args []string) error {
		id, err := intArg("id", args[0])
		if err != nil {
			return err
		}
		return env.show(api.Request{Method: "POST", Path: "/connections/" + string(id) + "/resource",
			Body: pyBody(pyjson.Ordered("resource_ref", args[1])), Header: jsonHeader})
	}
	rm := leaf("rm <id>", "Disconnect (bound tools stay, but stop working until reconnected).", "treg connections rm 12")
	rm.Args = cobra.ExactArgs(1)
	rm.RunE = func(_ *cobra.Command, args []string) error {
		id, err := intArg("id", args[0])
		if err != nil {
			return err
		}
		return env.show(api.Request{Method: "DELETE", Path: "/connections/" + string(id)})
	}
	cmd.AddCommand(ls,
		connectCmd(env, "treg connections", leaf("connect [name]",
			"Connect a provider: browser OAuth consent - or a pasted API key for key-based providers.",
			"treg connections connect --provider google-search-console          # treg's app, read scope",
			"treg connections connect --provider google-search-console --capability write",
			"treg connections connect --provider hunter                         # a key: prompts, input hidden",
			`echo "$HUNTER_KEY" | treg connections connect --provider hunter --key-stdin   # agents/CI`,
			"treg connections connect gsc --client-secret ./client_secret.json --scopes <scope>  # your own app")),
		providersCmd(env, leaf("providers", "List providers treg holds its own OAuth app for (what you can connect).",
			"treg connections providers")),
		resources, use, rm)
	return cmd
}

// oauthCmd is the hidden back-compat spelling of `connections connect` and `connections providers`.
func oauthCmd(env *Env) *cobra.Command {
	cmd := group("oauth", "(alias) `treg connections connect` / `treg connections providers`.")
	cmd.Hidden = true
	cmd.AddCommand(
		providersCmd(env, leaf("providers", "List providers treg holds its own OAuth app for.", "treg oauth providers")),
		connectCmd(env, "treg oauth", leaf("connect [name]",
			"(alias) Connect a provider: browser consent, or a pasted key for key-based providers.",
			"treg oauth connect --provider google-search-console")))
	return cmd
}

func providersCmd(env *Env, cmd *cobra.Command) *cobra.Command {
	cmd.Args = cobra.NoArgs
	cmd.RunE = func(*cobra.Command, []string) error {
		return env.show(api.Request{Path: "/oauth/providers"})
	}
	return cmd
}

type connectFlags struct {
	provider, capability, clientSecret string
	scopes                             []string
	keyStdin                           bool
	name                               string
}

func connectCmd(env *Env, prefix string, cmd *cobra.Command) *cobra.Command {
	var f connectFlags
	fl := cmd.Flags()
	fl.StringVar(&f.provider, "provider", "", "a registry service id - see `"+prefix+" providers`")
	fl.StringVar(&f.capability, "capability", "", "scope set to request (default: provider-specific)")
	fl.StringVar(&f.clientSecret, "client-secret", "", "path to your own OAuth client-secret JSON (bring-your-own-app)")
	fl.StringArrayVar(&f.scopes, "scopes", nil, "one or more OAuth scopes (with --client-secret)")
	fl.BoolVar(&f.keyStdin, "key-stdin", false, "a pasted-key provider: read the key from stdin (agents/CI) instead of a hidden prompt")
	// --scopes takes every word after it, as argparse's nargs="+" does, so this command reads its
	// own arguments.
	cmd.DisableFlagParsing = true
	cmd.RunE = func(cmd *cobra.Command, raw []string) error {
		args, err := parseScopes(cmd.Flags(), raw)
		if err != nil {
			return err
		}
		if help, _ := cmd.Flags().GetBool("help"); help {
			return cmd.Help()
		}
		if len(args) > 1 {
			return fmt.Errorf("unrecognized arguments: %s", strings.Join(args[1:], " "))
		}
		if len(args) == 1 {
			f.name = args[0]
		}
		return env.connect(f)
	}
	return cmd
}

// parseScopes parses a connect command line, where a bare --scopes consumes every following word
// that is not an option.
func parseScopes(fs *pflag.FlagSet, raw []string) ([]string, error) {
	var rewritten []string
	for i := 0; i < len(raw); i++ {
		if raw[i] == "--" {
			rewritten = append(rewritten, raw[i:]...)
			break
		}
		if raw[i] != "--scopes" {
			rewritten = append(rewritten, raw[i])
			continue
		}
		n := 0
		for i+1 < len(raw) && !looksLikeOption(raw[i+1]) {
			i++
			n++
			rewritten = append(rewritten, "--scopes="+raw[i])
		}
		if n == 0 {
			return nil, errors.New("argument --scopes: expected at least one argument")
		}
	}
	if err := fs.Parse(rewritten); err != nil {
		return nil, err
	}
	return fs.Args(), nil
}

// looksLikeOption is argparse's test for a word that starts an option rather than being a value.
func looksLikeOption(s string) bool {
	return strings.HasPrefix(s, "-") && s != "-" && !negativeNumber.MatchString(s) && !strings.Contains(s, " ")
}

var negativeNumber = regexp.MustCompile(`^-\d+$|^-\d*\.\d+$`)

func (e *Env) connect(f connectFlags) error {
	var b *pyjson.Object
	switch {
	case f.provider != "":
		if pasted := e.pastedKeyProvider(f.provider); pasted != nil {
			return e.connectPastedKey(f, pasted)
		}
		if f.keyStdin {
			return output.Fail("--key-stdin is for providers connected with a pasted key; %s "+
				"isn't one here (see `treg connections providers`)", f.provider)
		}
		b = pyjson.Ordered("provider", f.provider)
		if f.name != "" {
			b.Set("name", f.name)
		}
		if f.capability != "" {
			b.Set("capability", f.capability)
		}
	case f.clientSecret != "":
		var err error
		if b, err = byoBody(f); err != nil {
			return err
		}
	default:
		return output.Fail("give --provider <service> to use treg's app (see `treg connections providers`), " +
			"or --client-secret <file> to bring your own")
	}
	c := e.Client()
	r, err := e.do(c, api.Request{Method: "POST", Path: "/oauth/start", Body: pyBody(b), Header: jsonHeader})
	if err != nil {
		return err
	}
	if r.Status != 200 {
		return e.Out.Show(r)
	}
	d := responseObject(r)
	if guidance := d.Value("connect_guidance"); pyjson.Truthy(guidance) {
		outln("\n" + pyjson.Text(guidance))
	}
	outf("\n1. Ensure this redirect URI is allowed:\n   %s\n", pyjson.Text(d.Value("redirect_uri")))
	outf("\n2. Open to authorize:\n   %s\n\nWaiting…\n", pyjson.Text(d.Value("consent_url")))
	for range 150 {
		time.Sleep(2 * time.Second)
		poll, err := c.Do(api.Request{Path: "/oauth/status/" + pyjson.Text(d.Value("state"))})
		if err != nil {
			continue
		}
		v, err := pyjson.Decode(poll.Body)
		s, ok := v.(*pyjson.Object)
		if err != nil || !ok {
			continue
		}
		switch s.Value("status") {
		case "done":
			line := "✅ Connected. New oauth secret id: " + pyjson.Text(s.Value("secret_id"))
			if name := pyjson.Text(s.Value("name")); name != "" {
				line += " (" + name + ")"
			}
			outln(line)
			return nil
		case "error":
			return output.Fail("❌ Failed: %s", pyjson.Text(s.Value("detail")))
		}
	}
	return output.Fail("Timed out waiting for authorization.")
}

// pastedKeyProvider is the registry's entry for a service connected by pasting a key, else nil;
// an unreachable or unreadable listing is nil too, so /oauth/start gives the registry's answer.
func (e *Env) pastedKeyProvider(service string) *pyjson.Object {
	r, err := e.Client().Do(api.Request{Path: "/oauth/providers"})
	if err != nil || r.Status != 200 {
		return nil
	}
	v, err := pyjson.Decode(r.Body)
	listing, ok := v.([]any)
	if err != nil || !ok {
		return nil
	}
	for _, p := range listing {
		entry, ok := p.(*pyjson.Object)
		if !ok || entry.Value("service") != service {
			continue
		}
		if kind := entry.Value("auth_kind"); kind == "key" || kind == "token" {
			return entry
		}
		return nil
	}
	return nil
}

// connectPastedKey reads the key without echo, or from stdin with --key-stdin, so it never lands on
// the command line; stdin is read only when asked, as an agent's idle pipe would hang the read. A
// provider that signs with a pair reads both halves before sending either.
func (e *Env) connectPastedKey(f connectFlags, provider *pyjson.Object) error {
	label := pyjson.Text(orDefault(provider.Value("token_label"), "API key"))
	service := pyjson.Text(provider.Value("service"))
	display := pyjson.Text(orDefault(provider.Value("display_name"), service))
	extraLabel := ""
	if pyjson.Truthy(provider.Value("needs_extra_credential")) && pyjson.Truthy(provider.Value("extra_credential_label")) {
		extraLabel = pyjson.Text(provider.Value("extra_credential_label"))
	}
	if f.name != "" || f.capability != "" || f.clientSecret != "" || len(f.scopes) > 0 {
		return output.Fail("%s is connected with a pasted %s; a name, --capability, "+
			"--client-secret and --scopes don't apply", display, label)
	}
	var key, extra string
	switch {
	case f.keyStdin:
		if term.IsTerminal(os.Stdin) {
			return output.Fail("--key-stdin reads a piped %s; at a terminal, drop it to get a hidden prompt", label)
		}
		raw, _ := io.ReadAll(os.Stdin)
		piped := stdinText(raw)
		if extraLabel == "" {
			key = piped
			break
		}
		var lines []string
		for _, line := range pySplitLines(piped) {
			if l := providers.Strip(line); l != "" {
				lines = append(lines, l)
			}
		}
		if len(lines) > 0 {
			key = lines[0]
		}
		if len(lines) > 1 {
			extra = lines[1]
		}
	case term.IsTerminal(os.Stdin):
		if setup := provider.Value("setup_url"); pyjson.Truthy(setup) {
			outf("Get your %s: %s\n", label, pyjson.Text(setup))
		}
		var err error
		if key, err = prompt.Hidden(fmt.Sprintf("Paste your %s %s (input hidden): ", display, label)); err == nil && extraLabel != "" {
			extra, err = prompt.Hidden(fmt.Sprintf("Paste your %s %s (input hidden): ", display, extraLabel))
		}
		if err != nil {
			return output.Fail("\ncancelled - nothing was connected")
		}
	default:
		return output.Fail("no terminal to prompt for the %s: pipe it in with --key-stdin, e.g. "+
			"`pbpaste | treg connections connect --provider %s --key-stdin`", label, service)
	}
	if providers.Strip(key) == "" {
		return output.Fail("no %s given - nothing was connected", label)
	}
	if extraLabel != "" && providers.Strip(extra) == "" {
		hint := ""
		if f.keyStdin {
			hint = ": pipe it as the second line after the " + label
		}
		return output.Fail("%s also needs your %s%s - nothing was connected", display, extraLabel, hint)
	}
	c := e.Client()
	r, err := e.do(c, api.Request{Method: "POST", Path: "/connections/token",
		Body: pyBody(pyjson.Ordered("provider", service, "token", providers.Strip(key))), Header: jsonHeader})
	if err != nil {
		return err
	}
	if r.Status == 200 && extraLabel != "" {
		secretID := pyjson.Text(responseObject(r).Value("id"))
		r, err = e.do(c, api.Request{Method: "POST", Path: "/connections/" + secretID + "/extra-credential",
			Body: pyBody(pyjson.Ordered("value", providers.Strip(extra))), Header: jsonHeader})
		if err != nil {
			return err
		}
	}
	if r.Status != 200 {
		if err := e.Out.Show(r); err != nil {
			return err
		}
		// A 2xx other than 200 still connected nothing.
		return &output.Exit{Code: 1}
	}
	d := responseObject(r)
	outf("✅ Connected %s. Connection id: %s (%s)\n", display, pyjson.Text(d.Value("id")), pyjson.Text(d.Value("name")))
	return nil
}

// byoBody is a bring-your-own-app start request from the provider's OAuth client JSON on disk.
func byoBody(f connectFlags) (*pyjson.Object, error) {
	if f.name == "" {
		return nil, output.Fail("a name is required when bringing your own client secret")
	}
	text, err := readText(f.clientSecret)
	if err != nil {
		if _, ok := errors.AsType[*os.PathError](err); ok {
			return nil, output.Fail("could not read client-secret JSON %s: %s", quote(f.clientSecret), osError(err, f.clientSecret))
		}
		return nil, output.Fail("could not read client-secret JSON %s: %s", quote(f.clientSecret), err)
	}
	parsed, err := pyjson.Loads(text)
	if err != nil {
		return nil, output.Fail("could not read client-secret JSON %s: %s", quote(f.clientSecret), err)
	}
	cs, _ := parsed.(*pyjson.Object)
	var block any = cs
	if v := cs.Value("installed"); pyjson.Truthy(v) {
		block = v
	} else if v := cs.Value("web"); pyjson.Truthy(v) {
		block = v
	}
	o, ok := block.(*pyjson.Object)
	if !ok || !pyjson.Truthy(o.Value("client_id")) || !pyjson.Truthy(o.Value("client_secret")) {
		return nil, output.Fail("client-secret JSON is missing client_id / client_secret (expected a Google OAuth client file)")
	}
	get := func(key, fallback string) any {
		if v, ok := o.Get(key); ok {
			return v
		}
		return fallback
	}
	scopes := make([]any, len(f.scopes))
	for i, s := range f.scopes {
		scopes[i] = s
	}
	return pyjson.Ordered("name", f.name, "client_id", o.Value("client_id"), "client_secret", o.Value("client_secret"),
		"auth_uri", get("auth_uri", "https://accounts.google.com/o/oauth2/auth"),
		"token_uri", get("token_uri", "https://oauth2.googleapis.com/token"), "scopes", scopes), nil
}

// orDefault is Python's `value or fallback`.
func orDefault(v any, fallback string) any {
	if pyjson.Truthy(v) {
		return v
	}
	return fallback
}

// pySplitLines is Python's str.splitlines().
func pySplitLines(s string) []string {
	var out []string
	start := 0
	runes := []rune(s)
	for i := 0; i < len(runes); i++ {
		switch runes[i] {
		case '\n', '\v', '\f', '\x1c', '\x1d', '\x1e', '\u0085', '\u2028', '\u2029':
		case '\r':
			if i+1 < len(runes) && runes[i+1] == '\n' {
				out = append(out, string(runes[start:i]))
				i++
				start = i + 1
				continue
			}
		default:
			continue
		}
		out = append(out, string(runes[start:i]))
		start = i + 1
	}
	if start < len(runes) {
		out = append(out, string(runes[start:]))
	}
	return out
}
