package commands

import (
	"bufio"
	"cmp"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strings"
	"time"

	"github.com/cli/browser"
	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/buildinfo"
	"github.com/superdesigndev/treg/cli/internal/config"
	"github.com/superdesigndev/treg/cli/internal/output"
	"github.com/superdesigndev/treg/cli/internal/pyjson"
)

func versionCmd(env *Env) *cobra.Command {
	return &cobra.Command{
		Use:   "version",
		Short: "Print the installed treg version.",
		Args:  cobra.NoArgs,
		RunE: func(*cobra.Command, []string) error {
			outln("treg " + buildinfo.Version)
			return nil
		},
	}
}

func configCmd(env *Env) *cobra.Command {
	var baseURL, channel string
	cmd := &cobra.Command{
		Use:   "config",
		Short: "Show or set the registry this CLI talks to (base URL).",
		Example: "  treg config                                    # show current base URL\n" +
			"  treg config --base-url https://treg.to",
		Args: cobra.NoArgs,
		RunE: func(*cobra.Command, []string) error {
			cfg := env.Cfg
			if channel != "" && channel != "stable" && channel != "beta" {
				return output.Fail("treg: --channel is stable or beta")
			}
			if baseURL != "" || channel != "" {
				if baseURL != "" {
					cfg.BaseURL = baseURL
				}
				if channel != "" {
					cfg.Channel = channel
				}
				if err := env.Save(); err != nil {
					return err
				}
			}
			view := pyjson.Ordered("base_url", cfg.BaseURL, "email", nullable(cfg.Email),
				"active_org", nullable(cfg.ActiveOrg), "logged_in", cfg.Token != "")
			if cfg.Channel != "" {
				view.Set("channel", cfg.Channel)
			}
			env.Out.ShowJSON(view)
			return nil
		},
	}
	cmd.Flags().StringVar(&baseURL, "base-url", "", "point the CLI at this registry URL")
	cmd.Flags().StringVar(&channel, "channel", "", "the release channel `treg update` follows: stable or beta")
	return cmd
}

func logoutCmd(env *Env) *cobra.Command {
	return &cobra.Command{
		Use:   "logout",
		Short: "Clear the stored credentials for this machine.",
		Args:  cobra.NoArgs,
		RunE: func(*cobra.Command, []string) error {
			cfg := env.Cfg
			cfg.Token, cfg.Email, cfg.ActiveOrg, cfg.Identity = "", "", "", false
			if err := env.Save(); err != nil {
				return err
			}
			outln("Logged out.")
			return nil
		},
	}
}

func whoamiCmd(env *Env) *cobra.Command {
	return &cobra.Command{
		Use:   "whoami",
		Short: "Who this CLI acts as: your account, the active team and your role, the server.",
		Args:  cobra.NoArgs,
		RunE: func(*cobra.Command, []string) error {
			c := env.Client()
			me, err := env.do(c, api.Request{Path: "/auth/me"})
			if err != nil {
				return err
			}
			rows, err := env.orgRows(c)
			if err != nil {
				return err
			}
			active := env.pickOrg(rows)
			if me.Status == 401 {
				return output.Fail("treg: not signed in, or this token is invalid/expired.\n  Sign in:  treg login")
			}
			var who map[string]any
			if me.Status == 200 {
				_ = json.Unmarshal(me.Body, &who)
			}
			email := cmp.Or(str(who["email"]), env.Cfg.Email)
			if env.JSON {
				var team, role any
				if active != nil {
					team, role = active.Slug, nullable(active.Role)
				}
				env.Out.ShowJSON(pyjson.Ordered("email", nullable(email), "team", team, "role", role, "registry", env.Registry()))
				return nil
			}
			team := cmp.Or(env.EffectiveOrg(), "-")
			if active != nil {
				team = active.Slug
				if active.Role != "" {
					team = fmt.Sprintf("%s  (%s)", active.Slug, active.Role)
				}
			}
			env.kv("email", cmp.Or(email, "-"))
			env.kv("team", team)
			env.kv("server", env.Registry())
			return nil
		},
	}
}

// kv is one aligned `name value` line.
func (e *Env) kv(k, v string) {
	p := e.Colors()
	if len(k) >= 7 {
		outf("  %s%-7s%s %s\n", p.Muted, k, p.Reset, v)
	} else {
		outf("  %s%-7s%s%s\n", p.Muted, k, p.Reset, v)
	}
}

func loginCmd(env *Env) *cobra.Command {
	var token, email string
	cmd := &cobra.Command{
		Use:   "login",
		Short: "Sign in. Opens the browser sign-in page; --email for a terminal-only code; --token for agents/CI.",
		Example: "  treg login                                     # browser (reuses a dashboard session, or GitHub/Google/email)\n" +
			"  treg login --email you@company.com             # emailed 6-digit code\n" +
			"  treg login --token <per-org-token>             # non-interactive (agents/CI)",
		Args: cobra.NoArgs,
		RunE: func(*cobra.Command, []string) error {
			if token != "" {
				return env.loginWithToken(token)
			}
			if email != "" {
				done, err := env.loginWithEmail(email)
				if done || err != nil {
					return err
				}
			}
			return env.loginInBrowser()
		},
	}
	cmd.Flags().StringVar(&token, "token", "", "a per-org token (agents/CI) instead of the browser/email flow")
	cmd.Flags().StringVar(&email, "email", "", "sign in with a one-time code emailed to this address")
	return cmd
}

func (e *Env) loginWithToken(token string) error {
	cfg := e.Cfg
	scope := tokenClaim(token, "scp")
	cfg.Token, cfg.ActiveOrg = token, ""
	cfg.Identity = scope == "team" || scope == "bootstrap"
	c := api.New(e.Registry(), token, e.EffectiveOrg(), e.JSON)
	who, err := c.Do(api.Request{Path: "/auth/me"})
	if err != nil {
		return output.Fail("Could not reach %s to verify the token: %v", e.Registry(), err)
	}
	if who.Status == 401 {
		return output.Fail("That token was rejected (401 invalid token). It's expired or from a different " +
			"server - copy a fresh one from the dashboard ('API token' / the Access instruction).")
	}
	if who.Status >= 400 {
		return output.Fail("Token check failed (%d): %s", who.Status, clip(string(who.Body), 120))
	}
	var me map[string]any
	_ = json.Unmarshal(who.Body, &me)
	cfg.Email = str(me["email"])
	if err := e.Save(); err != nil {
		return err
	}
	e.pickActiveOrg(true)
	if cfg.ActiveOrg != "" {
		outln("✓ Token saved. Active org: " + cfg.ActiveOrg)
	} else {
		outln("✓ Token saved, but you're not in a team yet. Create one with " +
			"`treg org create \"Your Team\"` or accept an invite, then retry.")
	}
	return nil
}

// loginWithEmail runs the emailed-code sign-in. It reports done=false when the registry asks for a
// browser check, which a terminal cannot pass, so the browser handshake finishes the sign-in.
func (e *Env) loginWithEmail(email string) (bool, error) {
	c := e.AnonymousClient()
	start, err := e.do(c, api.Request{Method: "POST", Path: "/auth/email/start",
		Body: jsonBody(map[string]string{"email": email}), Header: jsonHeader})
	if err != nil {
		return true, err
	}
	if start.Status == 400 && start.Header.Get("X-Treg-Captcha") == "required" {
		outln("This server only emails sign-in codes to a browser. Continuing in the browser…")
		return false, nil
	}
	if start.Status >= 400 {
		return true, e.Out.Show(start)
	}
	var d map[string]any
	_ = json.Unmarshal(start.Body, &d)
	if code := str(d["dev_code"]); code != "" {
		outln("(dev) your code is: " + code)
	} else {
		outln("We sent a 6-digit code to " + email + ".")
	}
	outf("Enter code: ")
	line, _ := bufio.NewReader(os.Stdin).ReadString('\n')
	verify, err := e.do(c, api.Request{Method: "POST", Path: "/auth/email/verify",
		Body: jsonBody(map[string]string{"email": email, "code": strings.TrimSpace(line)}), Header: jsonHeader})
	if err != nil {
		return true, err
	}
	if verify.Status >= 400 {
		return true, e.Out.Show(verify)
	}
	_ = json.Unmarshal(verify.Body, &d)
	cfg := e.Cfg
	cfg.Token, cfg.Email, cfg.Identity = str(d["token"]), str(d["email"]), true
	// Persist the fresh token before the optional team lookup.
	if err := e.Save(); err != nil {
		return true, err
	}
	e.pickActiveOrg(false)
	e.pinTokenToActiveOrg(verify.Cookies)
	outln(loggedIn(cfg.Email, cfg.ActiveOrg))
	return true, nil
}

// loggedIn is the line a sign-in ends with; without a team it says how to get one, as a token
// sign-in does.
func loggedIn(email, org string) string {
	who := "✓ Logged in"
	if email != "" {
		who += " as " + email
	}
	if org == "" {
		return who + ". You're not in a team yet: create one with `treg org create \"Your Team\"` or accept an invite."
	}
	return who + ". Active org: " + org
}

var adLinkShape = regexp.MustCompile(`^[a-z0-9]{6}$`)

func (e *Env) loginInBrowser() error {
	base := strings.TrimRight(e.Registry(), "/")
	c := e.AnonymousClient()
	// The registry mints the login id and a short pairing code; the browser shows the code so the
	// person can match it against this terminal, and a login someone else started cannot be
	// approved into a token for them. A registry too old for /start gets a locally minted id.
	var lid, code string
	start, startErr := c.Do(api.Request{Method: "POST", Path: "/auth/cli/start"})
	if startErr == nil && start.Status == 200 {
		var j map[string]any
		_ = json.Unmarshal(start.Body, &j)
		lid, code = str(j["login_id"]), str(j["code"])
	}
	if lid == "" {
		lid = randomURLSafe(18)
	}
	if startErr != nil && isLoopback(base) {
		return output.Fail("Cannot reach %s - is a local treg server running?\n"+
			"  If you meant to use the production registry, run:\n"+
			"    treg config --base-url %s\n"+
			"  then retry `treg login`.\n"+
			"  (error: %v)", base, config.ProductionBaseURL, startErr)
	}
	ad := pendingAdLink()
	loginURL := base + "/login?cli=" + lid
	if ad != "" {
		loginURL += "&ad=" + ad
	}
	if code != "" {
		// In the fragment, never sent to the server, so it stays out of request logs.
		loginURL += "#code=" + code
	}
	outf("Opening your browser to sign in…\nIf it doesn't open, visit:\n  %s\n\n", loginURL)
	if code != "" {
		p := e.Colors()
		outf("  The sign-in page shows this code - check it matches:  %s%s%s%s\n\n", p.Bold, p.Teal, code, p.Reset)
	}
	outln("Waiting for authorization…")
	browser.Stdout, browser.Stderr = nil, nil
	_ = browser.OpenURL(loginURL)
	cfg := e.Cfg
	for range 180 {
		time.Sleep(time.Second)
		poll, err := c.Do(api.Request{Path: "/auth/cli/poll", Query: [][2]string{{"login_id", lid}}})
		if err != nil {
			continue
		}
		var d map[string]any
		if json.Unmarshal(poll.Body, &d) != nil || str(d["token"]) == "" {
			continue
		}
		cfg.Token, cfg.Email, cfg.Identity = str(d["token"]), str(d["email"]), true
		if org := str(d["active_org"]); org != "" {
			cfg.ActiveOrg = org
		}
		// The login id is single-use: persist before anything else can fail.
		if err := e.Save(); err != nil {
			return err
		}
		if ad != "" {
			_ = os.Remove(config.Sibling("ad-link"))
		}
		if cfg.ActiveOrg == "" {
			e.pickActiveOrg(true)
		} else {
			e.pinTokenToActiveOrg(nil)
		}
		outln(loggedIn(cfg.Email, cfg.ActiveOrg))
		return nil
	}
	return output.Fail("Login timed out - run `treg login` again.")
}

func pendingAdLink() string {
	raw, err := os.ReadFile(config.Sibling("ad-link"))
	if err != nil {
		return ""
	}
	v := strings.TrimSpace(string(raw))
	if !adLinkShape.MatchString(v) {
		return ""
	}
	return v
}

// pickActiveOrg sets the active team from /orgs, best effort: the token is already saved, so a
// transient failure must never lose it. With pin, the stored identity token is then exchanged for
// that team's Default key, so the token also works outside the CLI.
func (e *Env) pickActiveOrg(pin bool) {
	cfg := e.Cfg
	c := api.New(e.Registry(), api.Token(cfg), e.EffectiveOrg(), e.JSON)
	if r, err := c.Do(api.Request{Path: "/orgs"}); err == nil && r.Status == 200 {
		var orgs []map[string]any
		if json.Unmarshal(r.Body, &orgs) == nil && len(orgs) > 0 {
			claimed := tokenClaim(cfg.Token, "org")
			chosen := orgs[0]
			for _, o := range orgs {
				if str(o["slug"]) == claimed && claimed != "" {
					chosen = o
					break
				}
			}
			for _, o := range orgs {
				if o["active"] == true {
					chosen = o
					break
				}
			}
			cfg.ActiveOrg = str(chosen["slug"])
			_ = cfg.Save()
		}
	}
	if pin {
		e.pinTokenToActiveOrg(nil)
	}
}

// pinTokenToActiveOrg replaces a stored identity credential with the active team's Default key.
// An emailed-code login uses its fresh browser session for this exchange instead of the token.
func (e *Env) pinTokenToActiveOrg(session []*http.Cookie) {
	cfg := e.Cfg
	if cfg.ActiveOrg == "" || !cfg.Identity {
		return
	}
	if token, _ := e.defaultTokenForOrg(cfg.ActiveOrg, session); token != "" {
		cfg.Token = token
		_ = cfg.Save()
	}
}

// defaultTokenForOrg fetches one team's active Default key without changing the config.
func (e *Env) defaultTokenForOrg(org string, session []*http.Cookie) (string, string) {
	var c *api.Client
	if session != nil {
		c = e.AnonymousClient()
	} else {
		c = api.New(e.Registry(), api.Token(e.Cfg), e.EffectiveOrg(), e.JSON)
	}
	r, err := c.Do(api.Request{Path: "/auth/cli-token", Header: [][2]string{{"X-Treg-Org", org}}, Cookies: session})
	if err != nil {
		return "", "could not reach the registry"
	}
	var data map[string]any
	_ = json.Unmarshal(r.Body, &data)
	if r.Status != 200 {
		if d := str(data["detail"]); d != "" {
			return "", d
		}
		return "", fmt.Sprintf("the registry returned %d", r.Status)
	}
	if str(data["org"]) != org || str(data["token"]) == "" {
		return "", "the registry did not return this team's Default key"
	}
	if state, ok := data["default_key_state"]; ok && state != nil && state != "active" {
		return "", fmt.Sprintf("this team's Default key is %v", state)
	}
	return str(data["token"]), ""
}

// tokenClaim reads one claim from a signed token's payload without verifying it: it only picks a
// local default, and the registry authorizes every call.
func tokenClaim(token, claim string) string {
	payload, _, _ := strings.Cut(token, ".")
	raw, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(payload, "="))
	if err != nil {
		return ""
	}
	var claims map[string]any
	if json.Unmarshal(raw, &claims) != nil {
		return ""
	}
	return str(claims[claim])
}

func isLoopback(raw string) bool {
	u, err := url.Parse(raw)
	if err != nil {
		return false
	}
	h := u.Hostname()
	return h == "localhost" || h == "127.0.0.1" || h == "::1" || strings.HasPrefix(h, "127.")
}

func randomURLSafe(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return base64.RawURLEncoding.EncodeToString(b)
}
