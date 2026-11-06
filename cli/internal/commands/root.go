package commands

import (
	"os"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/buildinfo"
	"github.com/superdesigndev/treg/cli/internal/output"
)

const rootDescription = "treg - the tool catalog for your agent.\n" +
	"Call the tool a job needs without owning its API key: catalogued\n" +
	"endpoints priced per call, plus your team's own keys, skills and CLIs.\n" +
	"Credentials are injected server-side, never on your machine."

// Root is the command tree for one invocation.
func Root(env *Env) *cobra.Command {
	root := &cobra.Command{
		Use:           "treg",
		Short:         "The tool catalog for your agent.",
		Long:          rootDescription,
		Version:       buildinfo.Version,
		SilenceErrors: true,
		SilenceUsage:  true,
		Args:          cobra.NoArgs,
		PersistentPreRunE: func(cmd *cobra.Command, _ []string) error {
			return env.useOrgOnce(cmd)
		},
		RunE: func(cmd *cobra.Command, _ []string) error {
			return cmd.Help()
		},
	}
	root.SetVersionTemplate("treg {{.Version}}\n")
	root.SetHelpFunc(frontPage(root))
	root.CompletionOptions.DisableDefaultCmd = true
	root.AddCommand(
		versionCmd(env),
		configCmd(env),
		loginCmd(env),
		logoutCmd(env),
		whoamiCmd(env),
		callCmd(env),
		updateCmd(env),
		backgroundUpdateCmd(env),
		skillCmd(env),
		mcpCmd(env),
	)
	root.AddCommand(uploadCmds(env)...)
	return root
}

// useOrgOnce: a Default key belongs to one team, so `--org <other>` with it would be refused. For
// this one command, use the other team's Default key, the exchange `treg org use` makes, unsaved.
func (e *Env) useOrgOnce(cmd *cobra.Command) error {
	cfg := e.Cfg
	org := e.OrgOverride
	if org == "" || !cfg.Identity || cfg.Token == "" || os.Getenv("TREG_TOKEN") != "" ||
		org == cfg.ActiveOrg || cmd.CommandPath() == "treg org use" {
		return nil
	}
	token, detail := e.defaultTokenForOrg(org, nil)
	if token == "" {
		return output.Fail("--org %s: %s. Your teams: `treg org ls`.", org, detail)
	}
	cfg.UseOnce(token, org)
	return nil
}
