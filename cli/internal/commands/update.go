package commands

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/buildinfo"
	"github.com/superdesigndev/treg/cli/internal/output"
	"github.com/superdesigndev/treg/cli/internal/telemetry"
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
			if _, none := errors.AsType[*update.NoRelease](err); none {
				outf("The %s channel has no release yet, so this stays treg %s; it updates to the channel's "+
					"first release newer than this one.\n", env.channel(), buildinfo.Version)
				return nil
			}
			if err != nil {
				return output.Fail("treg: could not check for an update: %v", err)
			}
			if m.Version == buildinfo.Version || (!switched && !update.Newer(m.Version, buildinfo.Version)) {
				outf("treg %s is the newest release on the %s channel.\n", buildinfo.Version, env.channel())
				return nil
			}
			errln(fmt.Sprintf("Downloading treg %s...", m.Version))
			d, err := update.Install(context.Background(), m, exe)
			if err != nil {
				return output.Fail("treg: update failed: %v", err)
			}
			if d.Source == "mirror" {
				errln(fmt.Sprintf("(the first download failed: %s; used the mirror)", d.Detail))
			}
			outf("Updated treg %s -> %s (%s channel).\n", buildinfo.Version, m.Version, env.channel())
			refreshSkills(exe, false)
			return nil
		},
	}
	cmd.Flags().StringVar(&channel, "channel", "", "switch to this release channel: stable or beta")
	return cmd
}

// backgroundUpdateCmd is what a command starts, detached, once a day: it never prints, keeps its
// outcome for `treg update` to report, and records it as a usage event the next command sends.
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
			event := map[string]any{"from_version": buildinfo.Version}
			report := func(outcome, result string) {
				event["outcome"] = outcome
				update.RecordResult(result)
				telemetry.Update(event, env.Cfg.Channel)
			}
			m, err := update.Latest(context.Background(), env.Registry(), env.channel())
			if _, none := errors.AsType[*update.NoRelease](err); none {
				report("no_release", "no release on the "+env.channel()+" channel yet")
				return nil
			}
			if err != nil {
				event["detail"] = checkFailure(err)
				report("check_failed", "check failed: "+err.Error())
				return &output.Exit{Code: 1}
			}
			event["to_version"] = m.Version
			if !update.Newer(m.Version, buildinfo.Version) {
				report("up_to_date", "up to date at "+buildinfo.Version)
				return nil
			}
			d, err := update.Install(context.Background(), m, exe)
			event["source"], event["detail"], event["mirror_detail"] = d.Source, d.Detail, d.MirrorDetail
			if d.Detail != "" {
				event["seconds"] = d.Seconds
			}
			if f, ok := errors.AsType[*update.Failure](err); ok {
				event["reason"] = f.Step
				if d.Detail == "" {
					event["detail"] = f.Detail
				}
				report("failed", "update to "+m.Version+" failed: "+err.Error())
				return &output.Exit{Code: 1}
			}
			report("updated", "updated "+buildinfo.Version+" -> "+m.Version)
			refreshSkills(exe, true)
			return nil
		},
	}
}

// checkFailure is why asking the registry for the release failed, in a usage event's words.
func checkFailure(err error) string {
	if s, ok := errors.AsType[*update.CheckStatus](err); ok {
		return fmt.Sprintf("http_%d", s.Code)
	}
	return api.FailureKind(err)
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
