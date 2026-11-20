package commands

import (
	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/prompt"
)

func healthCmd(env *Env) *cobra.Command {
	var run bool
	cmd := leaf("health", "Show tool/secret health, or run the checks now with --run.", "treg health", "treg health --run")
	cmd.Args = cobra.NoArgs
	cmd.Flags().BoolVar(&run, "run", false, "run every tool's health check now")
	cmd.RunE = func(*cobra.Command, []string) error {
		if !run {
			return env.show(api.Request{Path: "/health"})
		}
		stop := prompt.Spin("running health checks against each provider")
		r, err := env.do(env.Client(), api.Request{Method: "POST", Path: "/health/run"})
		stop()
		if err != nil {
			return err
		}
		return env.Out.Show(r)
	}
	return cmd
}
