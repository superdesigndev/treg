package commands

import (
	"encoding/json/v2"
	"fmt"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/agents"
	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/ojson"
	"github.com/superdesigndev/treg/cli/internal/output"
	"github.com/superdesigndev/treg/cli/internal/term"
)

// setupScreen prints the sectioned human output of the machine-setup commands.
type setupScreen struct{ p term.Palette }

func (s setupScreen) section(title string) {
	bar := s.p.Accent + strings.Repeat("─", 58) + s.p.Reset
	outf("\n%s\n %s%s%s\n%s\n", bar, s.p.Bold, title, s.p.Reset, bar)
}

func (s setupScreen) ok(text string) { outf("  %s✓%s %s\n", s.p.Green, s.p.Reset, text) }

func (s setupScreen) dim(text string) { outf("%s%s%s\n", s.p.Muted, text, s.p.Reset) }

func (s setupScreen) arrow(text string) {
	outf("  %s→%s %s%s%s\n", s.p.Accent, s.p.Reset, s.p.Muted, text, s.p.Reset)
}

func mcpCmd(env *Env) *cobra.Command {
	mcp := &cobra.Command{
		Use:     "mcp",
		Short:   "Register the treg MCP server into the coding agents on this machine.",
		Example: "  treg mcp install",
		Args:    cobra.NoArgs,
		RunE:    missingSubcommand,
	}
	var name string
	var oauth bool
	install := &cobra.Command{
		Use: "install",
		Short: "Add treg as a header-authed MCP server in every supported detected agent " +
			"(or rewrite to OAuth with --oauth).",
		Example: "  treg mcp install",
		Args:    cobra.NoArgs,
		RunE: func(*cobra.Command, []string) error {
			return env.mcpInstall(name, oauth)
		},
	}
	install.Flags().StringVar(&name, "name", "treg", "the MCP server name to register (default: treg)")
	install.Flags().BoolVar(&oauth, "oauth", false, "rewrite every managed agent to URL-only (no Authorization) so the client "+
		"can authorize itself; needs no login - use when the saved MCP key is dead")
	grants := &cobra.Command{
		Use:     "grants",
		Short:   "List the MCP connections you've authorised and which team each one spends from.",
		Example: "  treg mcp grants",
		Args:    cobra.NoArgs,
		RunE:    func(*cobra.Command, []string) error { return env.mcpGrants() },
	}
	useTeam := &cobra.Command{
		Use:     "use-team <grant> <team>",
		Short:   "Point an authorised MCP connection at another of your teams (no reconnect).",
		Example: "  treg mcp use-team a1b2c3d4 superdesign",
		Args:    cobra.ExactArgs(2),
		RunE:    func(_ *cobra.Command, args []string) error { return env.mcpUseTeam(args[0], args[1]) },
	}
	mcp.AddCommand(install, grants, useTeam)
	return mcp
}

// verifiedMCPToken is the token mcp install writes, checked against /auth/me first: a stale or
// mangled key written into every agent fails days later inside the agent, looking like an outage.
func (e *Env) verifiedMCPToken() (string, error) {
	token := api.Token(e.Cfg)
	if token == "" {
		return "", output.Fail("no token - run `treg login` first (or `treg login --token <key>`), then retry")
	}
	if tokenClaim(token, "scp") == "bootstrap" {
		return "", output.Fail("This temporary login token cannot be installed into MCP - nothing was written. " +
			"Choose or create a team first, then retry with its Default or Agent key.")
	}
	c := e.Client()
	var who *api.Response
	var lastErr error
	for attempt := range 3 {
		r, err := c.Do(api.Request{Path: "/auth/me"})
		if err == nil {
			who = r
			break
		}
		lastErr = err
		if attempt < 2 {
			time.Sleep(time.Duration(1<<attempt) * time.Second)
		}
	}
	base := e.Registry()
	if who == nil {
		return "", output.Fail("Could not reach %s to verify the token: %v\n"+
			"  If this is an SSL or connection error, retry in a moment.\n"+
			"  If it persists, check your network or try: curl -I %s", base, lastErr, base)
	}
	if who.Status == 401 {
		return "", output.Fail("That token was rejected (401 invalid token) - nothing was written. It's expired " +
			"or from a different server; run `treg login` (or copy a fresh token from the " +
			"dashboard), then retry.")
	}
	if who.Status >= 400 {
		return "", output.Fail("Token check failed (%d): %s - nothing was written.", who.Status, clip(string(who.Body), 120))
	}
	return token, nil
}

func (e *Env) mcpInstall(name string, oauth bool) error {
	token := ""
	if !oauth {
		t, err := e.verifiedMCPToken()
		if err != nil {
			return err
		}
		token = t
	}
	if name == "" {
		name = "treg"
	}
	out := agents.InstallMCP(e.Registry(), token, name, oauth)
	s := setupScreen{e.Colors()}
	p := s.p
	title := "Registered " + name
	if oauth {
		title = "Rewrote " + name + " for OAuth"
	}
	s.section(fmt.Sprintf("① %s  (%s)", title, out.URL))
	ok := 0
	for _, r := range out.Results {
		switch r.Status {
		case agents.OK:
			ok++
			s.ok(fmt.Sprintf("%s%s%s  %s%s%s", p.Bold, r.Display, p.Reset, p.Muted, r.Detail, p.Reset))
		case agents.Skipped:
			outf("  %s·%s %s  %sskipped: %s%s\n", p.Muted, p.Reset, r.Display, p.Muted, r.Detail, p.Reset)
		default:
			outf("  ✗ %s  %s%s%s\n", r.Display, p.Muted, r.Detail, p.Reset)
		}
	}
	if len(out.Results) == 0 {
		verb := "register"
		if oauth {
			verb = "rewrite"
		}
		s.dim("  no MCP-capable agent found on this machine; nothing to " + verb)
	}
	if len(out.Manual) > 0 {
		s.section("② By hand")
		for _, m := range out.Manual {
			outf("  %s⚠%s %s%s%s\n", p.Accent, p.Reset, p.Bold, m.Display, p.Reset)
			for _, line := range agents.Wrap(m.How, 76) {
				s.dim("      " + line)
			}
		}
	}
	s.section("③ Next")
	if oauth {
		e.kv("agents", fmt.Sprintf("%d rewritten without Authorization; restart each agent, then authorize treg", ok))
		s.arrow("Claude Code: /mcp, pick treg, Authenticate")
		s.arrow("Codex: codex mcp login " + name)
		s.arrow("Cursor / opencode: follow the client's sign-in prompt for treg")
		return nil
	}
	e.kv("agents", fmt.Sprintf("%d registered; restart an agent to pick the server up", ok))
	s.arrow("in the agent, say \"use the " + name + " MCP\": call, catalog_search, catalog_get, my_tools, balance" +
		" (and hub_create, hub_update, hub_mine where the hub is on)")
	return nil
}

// clipColumn is one line of at most width characters, whitespace collapsed, cut with "…".
func clipColumn(text string, width int) string {
	text = strings.Join(strings.Fields(text), " ")
	if len([]rune(text)) <= width {
		return text
	}
	return string([]rune(text)[:width-1]) + "…"
}

// mcpGrants prints each grant id whole, clipping every other column: the id is what
// `treg mcp use-team` takes, and a clipped one copied off the setupScreen answers 404.
func (e *Env) mcpGrants() error {
	r, err := e.do(e.Client(), api.Request{Path: "/oauth/grants"})
	if err != nil {
		return err
	}
	if r.Status != 200 || e.JSON {
		return e.Out.Show(r)
	}
	var rows []map[string]any
	_ = json.Unmarshal(r.Body, &rows)
	s := setupScreen{e.Colors()}
	if len(rows) == 0 {
		outln("no MCP connections authorised by this account")
		s.dim("connections made while signed in as somebody else are listed under THAT account")
		return nil
	}
	outf("  %-22s %-22s %-18s %-20s\n", "GRANT", "CLIENT", "TEAM", "GRANTED")
	for _, g := range rows {
		team := str(g["team"])
		if team == "" {
			team = "?"
		}
		outf("  %-22s %-22s %-18s %-20s\n", str(g["grant"]), clipColumn(str(g["client"]), 22),
			clipColumn(team, 18), clip(str(g["granted"]), 19))
	}
	s.dim("  point one at another team: treg mcp use-team " + str(rows[0]["grant"]) + " <team-slug>")
	return nil
}

func (e *Env) mcpUseTeam(grant, team string) error {
	r, err := e.do(e.Client(), api.Request{Method: "POST", Path: "/oauth/grants/" + grant + "/team",
		Body: ojson.Compact(map[string]string{"team": team}), Header: jsonHeader})
	if err != nil {
		return err
	}
	if r.Status != 200 || e.JSON {
		if err := e.Out.Show(r); err != nil {
			return err
		}
		if r.Status != 200 {
			return &output.Exit{Code: 1}
		}
		return nil
	}
	var body map[string]any
	_ = json.Unmarshal(r.Body, &body)
	p := e.Colors()
	line := fmt.Sprintf("%s now spends from %s%s%s", str(body["grant"]), p.Bold, str(body["team"]), p.Reset)
	if name := str(body["team_name"]); name != "" {
		line += " (" + name + ")"
	}
	outln(line)
	if note := str(body["note"]); note != "" {
		setupScreen{p}.dim("  " + note)
	}
	return nil
}
