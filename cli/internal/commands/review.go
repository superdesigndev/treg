package commands

import (
	"regexp"
	"unicode/utf8"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/providers"
	"github.com/superdesigndev/treg/cli/internal/pyjson"
)

var reviewUsefulness = []string{"useful", "partly", "not_useful", "not_sure"}

const reviewDescription = "Rate a catalog call after you have used its result; not_sure is fine when you cannot tell. " +
	"The reason may be quoted on the endpoint's catalog page without naming your team, so write " +
	"about the endpoint. Use feedback for anything confusing or wrong, omit private information, " +
	"credentials and raw requests, responses or logs, and keep going with the task afterward."

var callIDShape = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)

func reviewCmd(env *Env) *cobra.Command {
	var reason string
	cmd := leaf("review <call_id> <usefulness>", reviewDescription,
		`treg review CALL_ID useful --reason "Helped answer the question."`)
	cmd.Short = "Rate a catalog call after using its result."
	cmd.Args = cobra.ExactArgs(2)
	cmd.Flags().StringVar(&reason, "reason", "", "optional sanitized reason, 1-200 characters")
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		if err := choice("usefulness", args[1], reviewUsefulness...); err != nil {
			return err
		}
		callID := providers.Strip(args[0])
		if !callIDShape.MatchString(callID) {
			return feedbackError("invalid_call_id", "Use the call ID from the catalog call response.", nil)
		}
		b := pyjson.Ordered("call_id", callID, "usefulness", args[1])
		if cmd.Flags().Changed("reason") {
			reason = providers.Strip(reason)
			if n := utf8.RuneCountInString(reason); n < 1 || n > 200 {
				return feedbackError("invalid_reason", "Reason must contain 1-200 characters after trimming. Omit private data.", nil)
			}
			b.Set("reason", reason)
		}
		uncertain := "Could not confirm whether the review was saved. Retry with the same call ID to confirm."
		r, err := env.Client().Do(api.Request{Method: "POST", Path: "/reviews", Body: pyBody(b), Header: jsonHeader})
		if err != nil {
			return feedbackError("submission_unconfirmed", uncertain, nil)
		}
		if r.Status >= 400 {
			code, message := "submission_unconfirmed", uncertain
			switch r.Status {
			case 400:
				code, message = "not_catalog_call", "Reviews require a catalog call, not a team's own tool."
			case 401:
				code, message = "authentication_required", "Sign in with `treg login`, or check the configured token."
			case 403:
				code, message = "access_denied", "Check the active team and your token's permissions."
			case 404:
				code, message = "not_found", "Call record not found in the active team; it may not be written yet. Retry shortly."
			case 422:
				code, message = "invalid_review", "Check the fields with `treg review --help`. Omit private data."
			}
			return feedbackError(code, message, pyjson.Ordered("http_status", r.Status))
		}
		receipt, err := pyjson.Decode(r.Body)
		if err != nil {
			return feedbackError("invalid_response", uncertain, nil)
		}
		outln(pyjson.Dumps(receipt, pyjson.Pretty))
		return nil
	}
	return cmd
}
