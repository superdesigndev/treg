package commands

import (
	"encoding/json"
	"fmt"
	"strconv"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/output"
)

func balanceCmd(env *Env) *cobra.Command {
	var limit int
	cmd := &cobra.Command{
		Use:   "balance",
		Short: "Your team's prepaid balance: credit left, calls in flight, recent spend.",
		Example: "  treg balance\n" +
			"  treg balance --limit 50\n" +
			"  treg balance --json    # micro-USD integers",
		Args: cobra.NoArgs,
		RunE: func(*cobra.Command, []string) error {
			return env.balance(limit)
		},
	}
	cmd.Flags().IntVar(&limit, "limit", 20, "how many recent ledger rows (default: 20)")
	return cmd
}

func (e *Env) balance(limit int) error {
	c := e.Client()
	org, err := e.activeOrg(c, true)
	if err != nil {
		return err
	}
	if org == nil {
		return output.Fail("no active org")
	}
	resp, err := e.do(c, api.Request{Path: fmt.Sprintf("/orgs/%d/balance", org.OrgID),
		Query: [][2]string{{"limit", strconv.Itoa(limit)}}, Header: [][2]string{api.WithView}})
	if err != nil {
		return err
	}
	v, ok := resp.View()
	if !ok {
		return e.Out.ShowUnrendered(resp)
	}
	if e.JSON {
		return e.Out.Show(output.Data(resp, v))
	}
	return e.Out.ShowView(resp, v)
}

// activeOrg is the team this CLI acts in, from /orgs; an agent key, which /orgs refuses, names
// its one team in /auth/me. Nil when there is none; strict refuses a rejected token.
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
	if id, _ := body["org_id"].(float64); me.Status == 200 && id != 0 {
		return &orgRow{OrgID: int64(id), Slug: str(body["org"]), Role: str(body["role"])}, nil
	}
	if strict && me.Status == 401 {
		return nil, output.Fail("treg: not signed in, or this token is invalid/expired.\n" +
			"  Sign in:            treg login\n" +
			"  Using TREG_TOKEN?   check it is the token `org agent-new` printed.")
	}
	return nil, nil
}
