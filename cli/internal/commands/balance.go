package commands

import (
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
		Short: "Your team's prepaid balance: credit left, calls in flight, recent spend, and the daily cap on treg's keys.",
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
