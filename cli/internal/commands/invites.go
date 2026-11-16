package commands

import (
	"fmt"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/ojson"
	"github.com/superdesigndev/treg/cli/internal/output"
)

func invitesCmd(env *Env) *cobra.Command {
	cmd := leaf("invites", "List invites addressed to your email (accept with `treg accept`).", "treg invites")
	cmd.Args = cobra.NoArgs
	cmd.RunE = func(*cobra.Command, []string) error {
		r, err := env.do(env.Client(), api.Request{Path: "/invites/mine"})
		if err != nil {
			return err
		}
		return env.Out.Show(r)
	}
	return cmd
}

func acceptCmd(env *Env) *cobra.Command {
	cmd := leaf("accept <org>", "Accept an invite addressed to your email (no code needed).", "treg accept superdesign")
	cmd.Args = cobra.ExactArgs(1)
	cmd.RunE = func(_ *cobra.Command, args []string) error {
		c := env.Client()
		mine, err := env.do(c, api.Request{Path: "/invites/mine"})
		if err != nil {
			return err
		}
		if mine.Status != 200 {
			return env.Out.Show(mine)
		}
		id := ""
		for _, v := range responseList(mine) {
			row, _ := v.(*ojson.Object)
			if ojson.Text(row.Value("org")) == args[0] || ojson.Text(row.Value("id")) == args[0] {
				id = ojson.Text(row.Value("id"))
				break
			}
		}
		if id == "" {
			return output.Fail("no pending invite for %s - run `treg invites`", quote(args[0]))
		}
		r, err := env.do(c, api.Request{Method: "POST", Path: fmt.Sprintf("/invites/%s/accept", id)})
		if err != nil {
			return err
		}
		if r.Status == 200 {
			if err := env.saveJoinedTeam(r); err != nil {
				return err
			}
		}
		return env.Out.Show(r)
	}
	return cmd
}

func orgJoinCmd(env *Env) *cobra.Command {
	cmd := leaf("join <code>", "Join a team using an invite code (sign in as the invited email first).",
		"treg org join <code>")
	cmd.Args = cobra.ExactArgs(1)
	var email string
	cmd.Flags().StringVar(&email, "email", "", "optional: the invited email, checked against the account you are signed in as")
	cmd.RunE = func(_ *cobra.Command, args []string) error {
		if os.Getenv("TREG_TOKEN") == "" && env.Cfg.Token == "" {
			return output.Fail("sign in first: run `treg login` with the email the invite was sent to, then retry")
		}
		body := ojson.Ordered("code", args[0])
		if email != "" {
			body = ojson.Ordered("code", args[0], "email", email)
		}
		r, err := env.do(env.Client(), api.Request{Method: "POST", Path: "/invites/accept", Body: ojson.Compact(body), Header: jsonHeader})
		if err != nil {
			return err
		}
		if r.Status == 200 {
			if err := env.saveJoinedTeam(r); err != nil {
				return err
			}
		} else if r.Status == 403 && strings.Contains(string(r.Body), "different email") {
			errln("(this invite is for a different email - run `treg login` with that address, then retry)")
		}
		return env.Out.Show(r)
	}
	return cmd
}

// saveJoinedTeam keeps the joined team's token, unless this run's identity came from TREG_TOKEN:
// that token is per-process and must never overwrite the login saved in the config file.
func (e *Env) saveJoinedTeam(r *api.Response) error {
	if os.Getenv("TREG_TOKEN") != "" {
		return nil
	}
	d := responseObject(r)
	e.Cfg.Token, e.Cfg.ActiveOrg, e.Cfg.Identity = ojson.Text(d.Value("token")), ojson.Text(d.Value("org")), true
	return e.Save()
}
