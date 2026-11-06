package commands

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/output"
	"github.com/superdesigndev/treg/cli/internal/prompt"
	"github.com/superdesigndev/treg/cli/internal/providers"
	"github.com/superdesigndev/treg/cli/internal/pyjson"
	"github.com/superdesigndev/treg/cli/internal/term"
)

func orgCmd(env *Env) *cobra.Command {
	cmd := group("org", "Manage teams (orgs): create, switch, invite, members, join, leave, delete.",
		"treg org ls", `treg org create "Superdesign"`, "treg org use superdesign")
	cmd.Short = "Manage teams (orgs): create, switch, invite, members, join, leave."
	cmd.AddCommand(orgCreateCmd(env), orgLsCmd(env), orgUseCmd(env), orgMembersCmd(env),
		orgAgentNewCmd(env), orgAgentsCmd(env), orgAgentRmCmd(env))
	return cmd
}

func orgCreateCmd(env *Env) *cobra.Command {
	cmd := leaf("create <name>", "Create a team and become its owner.", `treg org create "Superdesign"`)
	cmd.Args = cobra.ExactArgs(1)
	cmd.RunE = func(_ *cobra.Command, args []string) error {
		c := env.Client()
		r, err := env.do(c, api.Request{Method: "POST", Path: "/orgs", Body: pyBody(pyjson.Ordered("name", args[0])), Header: jsonHeader})
		if err != nil {
			return err
		}
		if r.Status == 200 {
			d := responseObject(r)
			cfg := env.Cfg
			cfg.Token, cfg.ActiveOrg, cfg.Identity = pyjson.Text(d.Value("token")), pyjson.Text(d.Value("org")), true
			if err := env.Save(); err != nil {
				return err
			}
			if !env.JSON {
				p := env.Colors()
				fmt.Fprintf(os.Stderr, "  %sactive team is now%s %s   %s(switch back: treg org use <slug>)%s\n",
					p.Muted, p.Reset, pyjson.Text(d.Value("org")), p.Muted, p.Reset)
			}
		}
		return env.Out.Show(r)
	}
	return cmd
}

func orgLsCmd(env *Env) *cobra.Command {
	cmd := leaf("ls", "List the teams you belong to (marks the active one).", "treg org ls")
	cmd.Args = cobra.NoArgs
	cmd.RunE = func(*cobra.Command, []string) error {
		r, err := env.do(env.Client(), api.Request{Path: "/orgs"})
		if err != nil {
			return err
		}
		if r.Status != 200 || env.JSON {
			return env.Out.Show(r)
		}
		active := env.EffectiveOrg()
		for _, o := range responseList(r) {
			row, _ := o.(*pyjson.Object)
			slug := pyjson.Text(row.Value("slug"))
			mark, tail := " ", ""
			if slug == active {
				mark, tail = "*", "  (active)"
			}
			outf("%s %-22s %-22s %-7s%s\n", mark, slug, pyjson.Text(row.Value("name")), pyjson.Text(row.Value("role")), tail)
		}
		return nil
	}
	return cmd
}

func orgUseCmd(env *Env) *cobra.Command {
	cmd := leaf("use <slug>", "Switch the active team (used by later commands).", "treg org use superdesign")
	cmd.Args = cobra.ExactArgs(1)
	cmd.RunE = func(_ *cobra.Command, args []string) error {
		slug := args[0]
		cfg := env.Cfg
		// Validate before saving: a mistyped slug would otherwise be stored and fail every later
		// command with the registry's bare "choose an org".
		var slugs []string
		verified := false
		if r, err := env.Client().Do(api.Request{Path: "/orgs"}); err == nil && r.Status == 200 {
			verified = true
			for _, o := range responseList(r) {
				row, _ := o.(*pyjson.Object)
				if v, ok := row.Get("slug"); ok {
					slugs = append(slugs, pyjson.Text(v))
				} else {
					slugs = append(slugs, "")
				}
			}
			sort.Strings(slugs)
		}
		if verified {
			if !contains(slugs, slug) {
				teams := strings.Join(slugs, ", ")
				if teams == "" {
					teams = "(none)"
				}
				return output.Fail("you're not a member of %s - your teams: %s\nsee `treg org ls`; active org unchanged.", pyRepr(slug), teams)
			}
		} else {
			errln("warning: could not verify the team against the registry")
		}
		if cfg.Identity {
			// A team's Default key and another active team are an unusable pair, so the exchange
			// must succeed before either local value changes.
			token, detail := env.defaultTokenForOrg(slug, nil)
			if token == "" {
				return output.Fail("could not switch to %s: %s. Active team unchanged.", pyRepr(slug), detail)
			}
			cfg.Token, cfg.ActiveOrg = token, slug
		} else if cfg.ActiveOrg != slug {
			// An Additional or Agent key is fixed to its membership: it may confirm that team, never
			// switch.
			return output.Fail("this key cannot switch teams; run `treg login` as a human first. Active team unchanged.")
		}
		if err := env.Save(); err != nil {
			return err
		}
		outln("active org: " + slug)
		return nil
	}
	return cmd
}

func orgMembersCmd(env *Env) *cobra.Command {
	cmd := leaf("members", "List the active team's members and their roles.", "treg org members")
	cmd.Args = cobra.NoArgs
	cmd.RunE = func(*cobra.Command, []string) error {
		return env.inActiveOrg("no active org", func(c *api.Client, orgID int64) (api.Request, error) {
			return api.Request{Path: fmt.Sprintf("/orgs/%d/members", orgID)}, nil
		})
	}
	return cmd
}

func orgAgentsCmd(env *Env) *cobra.Command {
	cmd := leaf("agents", "List this team's agent identities and their limits (admin+). "+
		"(Different from `treg agents`, which lists coding agents for skill install.)", "treg org agents")
	cmd.Args = cobra.NoArgs
	cmd.RunE = func(*cobra.Command, []string) error {
		return env.inActiveOrg("no active org", func(c *api.Client, orgID int64) (api.Request, error) {
			return api.Request{Path: fmt.Sprintf("/orgs/%d/agents", orgID)}, nil
		})
	}
	return cmd
}

func orgAgentRmCmd(env *Env) *cobra.Command {
	cmd := leaf("agent-rm <user_id>", "Revoke an agent - its token stops working immediately.", "treg org agent-rm 7")
	cmd.Args = cobra.ExactArgs(1)
	cmd.RunE = func(_ *cobra.Command, args []string) error {
		userID, err := intArg("user_id", args[0])
		if err != nil {
			return err
		}
		return env.inActiveOrg("no active org", func(c *api.Client, orgID int64) (api.Request, error) {
			return api.Request{Method: "DELETE", Path: fmt.Sprintf("/orgs/%d/agents/%s", orgID, userID)}, nil
		})
	}
	return cmd
}

type agentFlags struct {
	role, tools, localRun, projects string
	cap                             string
	allTools, allProjects           bool
	pins                            []string
}

func orgAgentNewCmd(env *Env) *cobra.Command {
	var f agentFlags
	cmd := leaf("agent-new <name>", "Mint (or rotate) a token so an agent calls treg as ITSELF - its own "+
		"cap, tool access and audit trail (admin+).",
		"treg org agent-new ci-bot", "treg org agent-new ci-bot --tools stripe,gh --cap 500",
		"treg org agent-new ci-bot   # run again to rotate: the old token dies")
	cmd.Args = cobra.ExactArgs(1)
	fl := cmd.Flags()
	fl.StringVar(&f.role, "role", "member", "role to grant (default: member; an agent can never be an owner)")
	fl.StringVar(&f.cap, "cap", "-1", "daily call cap (-1 = unlimited, the default)")
	fl.StringVar(&f.tools, "tools", "", "comma-separated tool names this agent may use (default: prompt / all)")
	fl.BoolVar(&f.allTools, "all-tools", false, "allow every tool")
	fl.StringVar(&f.localRun, "local-run", "", "allow local CLI runs (on|off)")
	fl.StringVar(&f.projects, "projects", "", "comma-separated project slugs/ids this agent is scoped to")
	fl.BoolVar(&f.allProjects, "all-projects", false, "scope to every project (the default for a new agent)")
	fl.StringArrayVar(&f.pins, "pin", nil, "pin this token to a caller tag (repeatable), e.g. --pin customer=cust_A. "+
		"The pin beats whatever X-Treg-Meta the holder sends, so a token handed to one customer cannot bill another.")
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		if err := choice("--role", f.role, "viewer", "member", "admin"); err != nil {
			return err
		}
		dailyCap, err := intArg("--cap", f.cap)
		if err != nil {
			return err
		}
		if cmd.Flags().Changed("local-run") {
			if err := choice("--local-run", f.localRun, "on", "off"); err != nil {
				return err
			}
		}
		return env.inActiveOrg("no active org", func(c *api.Client, orgID int64) (api.Request, error) {
			access, err := env.resolveToolAccess(c, f.allTools, f.tools)
			if err != nil {
				return api.Request{}, err
			}
			b := pyjson.Ordered("name", args[0], "role", f.role, "daily_call_cap", dailyCap,
				"tool_access", access, "local_run_enabled", f.localRun != "off")
			// Scope is sent only when asked for, so a rotate never widens it or unpins the token.
			if f.allProjects {
				b.Set("project_access", nil)
			} else if f.projects != "" {
				b.Set("project_access", splitList(f.projects))
			}
			if len(f.pins) > 0 {
				pins := pyjson.NewObject()
				for _, item := range f.pins {
					dim, val, ok := strings.Cut(item, "=")
					if !ok || providers.Strip(dim) == "" || providers.Strip(val) == "" {
						return api.Request{}, output.Fail("--pin wants dim=value, got %s", pyRepr(item))
					}
					pins.Set(strings.ToLower(providers.Strip(dim)), providers.Strip(val))
				}
				b.Set("pinned_tags", pins)
			}
			return api.Request{Method: "POST", Path: fmt.Sprintf("/orgs/%d/agents", orgID), Body: pyBody(b), Header: jsonHeader}, nil
		})
	}
	return cmd
}

// splitList is a comma-separated flag as a list, blanks dropped.
func splitList(s string) []any {
	out := []any{}
	for _, part := range strings.Split(s, ",") {
		if p := providers.Strip(part); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// resolveToolAccess turns the access flags into the API's tool_access: nil for every tool, else
// the names allowed. Without a flag a person at a terminal is asked; anything else gets every tool.
func (e *Env) resolveToolAccess(c *api.Client, allTools bool, tools string) (any, error) {
	if allTools {
		return nil, nil
	}
	if tools != "" {
		return splitList(tools), nil
	}
	if !term.IsTerminal(os.Stdin) {
		return nil, nil
	}
	var names []string
	if r, err := e.do(c, api.Request{Path: "/tools"}); err != nil {
		return nil, err
	} else if r.Status == 200 {
		for _, t := range responseList(r) {
			row, _ := t.(*pyjson.Object)
			names = append(names, pyjson.Text(row.Value("name")))
		}
		sort.Strings(names)
	}
	if len(names) == 0 {
		return nil, nil
	}
	answer, err := prompt.Line(fmt.Sprintf("Give access to all %d tools? [Y/n]: ", len(names)))
	if err != nil {
		return nil, &output.Exit{Code: 1}
	}
	switch strings.ToLower(providers.Strip(answer)) {
	case "", "y", "yes":
		return nil, nil
	}
	choices := make([]prompt.Choice, len(names))
	for i, n := range names {
		choices[i] = prompt.Choice{Title: n, Checked: true}
	}
	picked, ok := prompt.Checkbox("Tools this member may use", choices)
	if !ok || len(picked) == len(names) {
		return nil, nil
	}
	chosen := make([]string, len(picked))
	for i, n := range picked {
		chosen[i] = names[n]
	}
	sort.Strings(chosen)
	out := make([]any, len(chosen))
	for i, n := range chosen {
		out[i] = n
	}
	return out, nil
}

// activeOrg is the membership this CLI acts in, from /orgs. An agent key is refused there on
// purpose, but its token is one membership, so /auth/me names the one team it could mean. With
// strict, an invalid token is reported here rather than as a bare "no active org".
func (e *Env) activeOrg(c *api.Client, strict bool) (*orgRow, error) {
	rows, err := e.orgRows(c)
	if err != nil {
		return nil, err
	}
	if rows != nil {
		return e.pickOrg(rows), nil
	}
	me, err := e.do(c, api.Request{Path: "/auth/me"})
	if err != nil {
		return nil, err
	}
	var body map[string]any
	_ = json.Unmarshal(me.Body, &body)
	if me.Status == 200 {
		if id := orgIDOf(body["org_id"]); id != 0 {
			return &orgRow{OrgID: id, Slug: str(body["org"]), Role: str(body["role"])}, nil
		}
	}
	if strict && me.Status == 401 {
		return nil, output.Fail("treg: not signed in, or this token is invalid/expired.\n" +
			"  Sign in:            treg login\n" +
			"  Using TREG_TOKEN?   check it is the token `org agent-new` printed.")
	}
	return nil, nil
}

// activeOrgID is the active team's numeric id, 0 when there is none.
func (e *Env) activeOrgID(c *api.Client, strict bool) (int64, error) {
	org, err := e.activeOrg(c, strict)
	if err != nil || org == nil {
		return 0, err
	}
	return org.OrgID, nil
}

func orgIDOf(v any) int64 {
	switch t := v.(type) {
	case float64:
		return int64(t)
	case string:
		n, _ := strconv.ParseInt(strings.TrimSpace(t), 10, 64)
		return n
	}
	return 0
}

// inActiveOrg sends one request about the active team and shows the answer; without a team it
// stops with missing.
func (e *Env) inActiveOrg(missing string, build func(*api.Client, int64) (api.Request, error)) error {
	c := e.Client()
	orgID, err := e.activeOrgID(c, true)
	if err != nil {
		return err
	}
	if orgID == 0 {
		return output.Fail("%s", missing)
	}
	req, err := build(c, orgID)
	if err != nil {
		return err
	}
	r, err := e.do(c, req)
	if err != nil {
		return err
	}
	return e.Out.Show(r)
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

// pyBody is a request body as httpx encodes `json=`.
func pyBody(v any) []byte { return []byte(pyjson.Dumps(v, pyjson.Body)) }

// responseObject is a response's JSON object in key order, empty when it is not one.
func responseObject(r *api.Response) *pyjson.Object {
	v, _ := pyjson.Decode(r.Body)
	if o, ok := v.(*pyjson.Object); ok {
		return o
	}
	return pyjson.NewObject()
}

// responseList is a list-returning endpoint's rows, empty when the body is not a list.
func responseList(r *api.Response) []any {
	v, _ := pyjson.Decode(r.Body)
	if l, ok := v.([]any); ok {
		return l
	}
	return nil
}
