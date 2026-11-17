package commands

import (
	"fmt"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/api"
)

func resourcesCmd(env *Env) *cobra.Command {
	cmd := group("resources", "Resources created on treg-provided accounts, scoped to your team.",
		"treg resources list --provider fishaudio --kind voice")
	var provider, kind string
	var includeDeleted bool
	list := leaf("list", "List this team's durable provider resources.", "treg resources list --provider fishaudio --kind voice")
	list.Args = cobra.NoArgs
	list.Flags().StringVar(&provider, "provider", "", "filter by provider id")
	list.Flags().StringVar(&kind, "kind", "", "filter by resource kind")
	list.Flags().BoolVar(&includeDeleted, "include-deleted", false, "include deleted tombstones")
	list.RunE = func(*cobra.Command, []string) error {
		return env.inActiveOrg("no active org - run `treg org use <slug>`", func(_ *api.Client, orgID int64) (api.Request, error) {
			var query [][2]string
			if provider != "" {
				query = append(query, [2]string{"provider", provider})
			}
			if kind != "" {
				query = append(query, [2]string{"kind", kind})
			}
			if includeDeleted {
				query = append(query, [2]string{"include_deleted", "true"})
			}
			return api.Request{Path: fmt.Sprintf("/orgs/%d/provider-resources", orgID), Query: query}, nil
		})
	}
	cmd.AddCommand(list)
	return cmd
}
