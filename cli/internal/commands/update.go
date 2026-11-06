package commands

import (
	"context"
	"fmt"
	"os"
	"os/exec"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/buildinfo"
	"github.com/superdesigndev/treg/cli/internal/output"
	"github.com/superdesigndev/treg/cli/internal/update"
)

func (e *Env) channel() string {
	if e.Cfg.Channel == "beta" {
		return "beta"
	}
	return "stable"
}

func updateCmd(env *Env) *cobra.Command {
	var channel string
	cmd := &cobra.Command{
		Use:   "update",
		Short: "Upgrade the treg CLI in place to the newest release on its channel.",
		Example: "  treg update                    # the newest release on this machine's channel\n" +
			"  treg update --channel beta     # join the beta channel\n" +
			"  treg update --channel stable   # leave it",
		Args: cobra.NoArgs,
		RunE: func(*cobra.Command, []string) error {
			if channel != "" && channel != "stable" && channel != "beta" {
				return output.Fail("treg: --channel is stable or beta")
			}
			exe, err := update.Executable()
			if err != nil {
				return output.Fail("treg: cannot find this executable: %v", err)
			}
			if method := update.Method(exe); method != "self" {
				outf("treg is installed with %s; upgrade it with: %s\n", method, update.UpgradeHint(method))
				return nil
			}
			switched := channel != "" && channel != env.channel()
			if channel != "" {
				env.Cfg.Channel = channel
				if err := env.Save(); err != nil {
					return err
				}
			}
			m, err := update.Latest(context.Background(), env.Registry(), env.channel())
			if err != nil {
				return output.Fail("treg: could not check for an update: %v", err)
			}
			if !switched && !update.Newer(m.Version, buildinfo.Version) {
				outf("treg %s is the newest release on the %s channel.\n", buildinfo.Version, env.channel())
				return nil
			}
			errln(fmt.Sprintf("Downloading treg %s...", m.Version))
			if err := update.Install(context.Background(), m, exe); err != nil {
				return output.Fail("treg: update failed: %v", err)
			}
			outf("Updated treg %s -> %s (%s channel).\n", buildinfo.Version, m.Version, env.channel())
			refreshSkills(exe, false)
			return nil
		},
	}
	cmd.Flags().StringVar(&channel, "channel", "", "switch to this release channel: stable or beta")
	return cmd
}

// backgroundUpdateCmd is what a command starts, detached, once a day: it never prints, and keeps
// its outcome for `treg update` to report.
func backgroundUpdateCmd(env *Env) *cobra.Command {
	return &cobra.Command{
		Use:    "__update",
		Hidden: true,
		Args:   cobra.NoArgs,
		RunE: func(*cobra.Command, []string) error {
			exe, err := update.Executable()
			if err != nil {
				return &output.Exit{Code: 1}
			}
			update.RemovePythonCLI(exe)
			m, err := update.Latest(context.Background(), env.Registry(), env.channel())
			if err != nil {
				update.RecordResult("check failed: " + err.Error())
				return &output.Exit{Code: 1}
			}
			if !update.Newer(m.Version, buildinfo.Version) {
				update.RecordResult("up to date at " + buildinfo.Version)
				return nil
			}
			if err := update.Install(context.Background(), m, exe); err != nil {
				update.RecordResult("update to " + m.Version + " failed: " + err.Error())
				return &output.Exit{Code: 1}
			}
			update.RecordResult("updated " + buildinfo.Version + " -> " + m.Version)
			refreshSkills(exe, true)
			return nil
		},
	}
}

// refreshSkills re-runs the skill bootstrap with the new binary, as re-running the installer did,
// so the SKILL.md copies agents read describe the CLI they now call.
func refreshSkills(exe string, quiet bool) {
	cmd := exec.Command(exe, "skill", "bootstrap")
	if !quiet {
		cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	}
	_ = cmd.Run()
}
