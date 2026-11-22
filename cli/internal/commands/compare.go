package commands

import (
	"strconv"
	"unicode/utf8"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/ojson"
	"github.com/superdesigndev/treg/cli/internal/providers"
)

var compareChoices = []string{"1", "2", "tie", "both_bad"}

const compareDescription = "Judge two anonymous answers to an input your team sent earlier. With no " +
	"arguments it shows the waiting pair; with a comparison ID and a choice it records one verdict " +
	"(1, 2, tie or both_bad) for the answer that serves the input better. Judge what the input asked " +
	"for, not length. No charge; keep going with the task afterward."

func compareCmd(env *Env) *cobra.Command {
	var reason string
	cmd := leaf("compare [comparison_id choice]", compareDescription,
		"treg compare",
		`treg compare 42 1 --reason "Answer 1 has the official site first."`)
	cmd.Short = "Judge two anonymous answers to an earlier input."
	cmd.Args = func(cmd *cobra.Command, args []string) error {
		if len(args) != 0 && len(args) != 2 {
			return feedbackError("invalid_arguments", "Run `treg compare` to see the pair, or "+
				"`treg compare COMPARISON_ID 1|2|tie|both_bad` to judge it.", nil)
		}
		return nil
	}
	cmd.Flags().StringVar(&reason, "reason", "", "optional one-line reason, 1-300 characters")
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		if len(args) == 0 {
			r, err := env.Client().Do(api.Request{Path: "/comparisons/next"})
			if err != nil {
				return feedbackError("unavailable", "Could not read the waiting comparison. Retry shortly.", nil)
			}
			if r.Status == 204 {
				outln(`{"status": "none_waiting"}`)
				return nil
			}
			return env.Out.Show(r)
		}
		id, err := strconv.Atoi(args[0])
		if err != nil || id < 1 {
			return feedbackError("invalid_comparison_id", "Use the comparison_id the pair was shown with.", nil)
		}
		if err := choice("choice", args[1], compareChoices...); err != nil {
			return err
		}
		b := ojson.Ordered("comparison_id", id, "choice", args[1])
		if cmd.Flags().Changed("reason") {
			reason = providers.Strip(reason)
			if n := utf8.RuneCountInString(reason); n < 1 || n > 300 {
				return feedbackError("invalid_reason", "Reason must contain 1-300 characters after trimming. Omit private data.", nil)
			}
			b.Set("reason", reason)
		}
		uncertain := "Could not confirm whether the verdict was saved. Retry with the same comparison ID to confirm."
		r, err := env.Client().Do(api.Request{Method: "POST", Path: "/comparisons", Body: ojson.Compact(b), Header: jsonHeader})
		if err != nil {
			return feedbackError("submission_unconfirmed", uncertain, nil)
		}
		if r.Status >= 400 {
			code, message := "submission_unconfirmed", uncertain
			switch r.Status {
			case 401:
				code, message = "authentication_required", "Sign in with `treg login`, or check the configured token."
			case 403:
				code, message = "access_denied", "Check the active team and your token's permissions."
			case 404:
				code, message = "not_found", "No such comparison waiting in the active team."
			case 422:
				code, message = "invalid_verdict", "Check the fields with `treg compare --help`. Omit private data."
			}
			return feedbackError(code, message, ojson.Ordered("http_status", r.Status))
		}
		receipt, err := ojson.Decode(r.Body)
		if err != nil {
			return feedbackError("invalid_response", uncertain, nil)
		}
		outln(ojson.Pretty(receipt))
		return nil
	}
	return cmd
}
