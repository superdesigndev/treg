package commands

import (
	"regexp"
	"strings"
	"unicode/utf8"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/ojson"
	"github.com/superdesigndev/treg/cli/internal/providers"
)

var reviewUsefulness = []string{"useful", "partly", "not_useful", "not_sure"}

const reviewDescription = "Rate a catalog call after you have used its result; not_sure is fine when you cannot tell. " +
	"The reason may be quoted on the endpoint's catalog page without naming your team, so write " +
	"about the endpoint. Use feedback for anything confusing or wrong, omit private information, " +
	"credentials and raw requests, responses or logs, and keep going with the task afterward. " +
	"When it was partly or not useful, add --rows with the 0-based rows you found wrong, e.g. " +
	"--rows 2/email:wrong,5:missing (INDEX[/FIELD]:VERDICT; FIELD:VERDICT or VERDICT alone for the " +
	"whole result). Only rows you looked at."

var callIDShape = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)

func reviewCmd(env *Env) *cobra.Command {
	var reason, rows string
	cmd := leaf("review <call_id> <usefulness>", reviewDescription,
		`treg review CALL_ID useful --reason "Helped answer the question."`+"\n"+
			`treg review CALL_ID partly --rows 2/email:wrong,5:missing`)
	cmd.Short = "Rate a catalog call after using its result."
	cmd.Args = cobra.ExactArgs(2)
	cmd.Flags().StringVar(&reason, "reason", "", "optional sanitized reason, 1-200 characters")
	cmd.Flags().StringVar(&rows, "rows", "", "rows you found wrong, comma-separated INDEX[/FIELD]:VERDICT (0-based)")
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		if err := choice("usefulness", args[1], reviewUsefulness...); err != nil {
			return err
		}
		callID := providers.Strip(args[0])
		if !callIDShape.MatchString(callID) {
			return feedbackError("invalid_call_id", "Use the call ID from the catalog call response.", nil)
		}
		b := ojson.Ordered("call_id", callID, "usefulness", args[1])
		if cmd.Flags().Changed("reason") {
			reason = providers.Strip(reason)
			if n := utf8.RuneCountInString(reason); n < 1 || n > 200 {
				return feedbackError("invalid_reason", "Reason must contain 1-200 characters after trimming. Omit private data.", nil)
			}
			b.Set("reason", reason)
		}
		if cmd.Flags().Changed("rows") {
			items, ok := parseReviewRows(rows)
			if !ok {
				return feedbackError("invalid_rows", "Write rows as INDEX[/FIELD]:VERDICT, comma-separated, e.g. 2/email:wrong,5:missing. "+
					"Verdicts: "+strings.Join(markVerdicts, ", ")+".", nil)
			}
			b.Set("rows", items)
		}
		uncertain := "Could not confirm whether the review was saved. Retry with the same call ID to confirm."
		r, err := env.Client().Do(api.Request{Method: "POST", Path: "/reviews", Body: ojson.Compact(b), Header: jsonHeader})
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

// parseReviewRows reads `2/email:wrong,5:missing` into mark items, at most 200, all or nothing.
func parseReviewRows(raw string) ([]any, bool) {
	var items []any
	for _, part := range strings.Split(raw, ",") {
		if strings.TrimSpace(part) == "" {
			continue
		}
		item, ok := parseMarkItem(part)
		if !ok {
			return nil, false
		}
		items = append(items, item)
	}
	return items, len(items) > 0 && len(items) <= 200
}
