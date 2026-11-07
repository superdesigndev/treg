package commands

import (
	"fmt"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/output"
	"github.com/superdesigndev/treg/cli/internal/pyjson"
)

func usageCmd(env *Env) *cobra.Command {
	var by, days string
	cmd := leaf("usage", "What each of your caller tags consumed - what you invoice your own users "+
		"from. Money comes from the ledger, so it is complete even when audit rows "+
		"were shed under load.",
		"treg usage                       # by your primary tag (default: customer)",
		"treg usage --by workspace",
		"treg usage --by customer --days 7")
	cmd.Short = "What each of your caller tags consumed."
	cmd.Args = cobra.NoArgs
	cmd.Flags().StringVar(&by, "by", "", "which caller tag to group by (default: your team's primary one)")
	cmd.Flags().StringVar(&days, "days", "30", "window in days (max 365)")
	cmd.RunE = func(*cobra.Command, []string) error {
		n, err := intArg("--days", days)
		if err != nil {
			return err
		}
		c := env.Client()
		orgID, err := env.activeOrgID(c, true)
		if err != nil {
			return err
		}
		if orgID == 0 {
			return output.Fail("no active org")
		}
		query := [][2]string{{"days", string(n)}}
		if by != "" {
			query = append(query, [2]string{"key", by})
		}
		r, err := env.do(c, api.Request{Path: fmt.Sprintf("/orgs/%d/usage/by-tag", orgID), Query: query})
		if err != nil {
			return err
		}
		if env.JSON || r.Status >= 400 {
			return env.Out.Show(r)
		}
		d := responseObject(r)
		p := env.Colors()
		key := d.Value("key")
		outf("\n  %sUsage by %s%s  %slast %s days%s\n", p.Accent, pyjson.Text(key), p.Reset, p.Muted, pyjson.Text(d.Value("days")), p.Reset)
		rows, _ := d.Value("rows").([]any)
		for _, item := range rows {
			row, _ := item.(*pyjson.Object)
			calls := row.Value("calls")
			plural := "s"
			if pyjson.Text(calls) == "1" {
				plural = ""
			}
			outf("    %-28s %10s  %s%s call%s%s\n", pyjson.Text(row.Value("value")), usd(row.Value("charged_micro")),
				p.Muted, pyjson.Text(calls), plural, p.Reset)
		}
		if len(rows) == 0 {
			outf("    %snothing tagged %s in this window.%s\n", p.Muted, quote(pyjson.Text(key)), p.Reset)
		}
		if pyjson.Truthy(d.Value("unattributed_micro")) {
			outf("    %s%-28s %10s%s\n", p.Muted, "(unattributed)", usd(d.Value("unattributed_micro")), p.Reset)
		}
		outf("\n    %-28s %s%10s%s\n\n", "total", p.Green, usd(d.Value("total_micro")), p.Reset)
		return nil
	}
	return cmd
}
