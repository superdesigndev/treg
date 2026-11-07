package commands

import (
	"fmt"
	"io"
	"os"
	"unicode/utf8"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/output"
	"github.com/superdesigndev/treg/cli/internal/providers"
	"github.com/superdesigndev/treg/cli/internal/pyjson"
	"github.com/superdesigndev/treg/cli/internal/term"
)

var feedbackCategories = []string{"quality", "pricing", "friction", "other"}

const feedbackDescription = "Proactively share problems and suggestions about treg. Small annoyances that slowed " +
	"your task down are useful feedback too: confusing results or charges, unclear " +
	"instructions, unhelpful errors, or missing capabilities you worked around. " +
	"Report these even if the task succeeded. " +
	"Describe what you needed and what you observed; you do not need to prove a bug. " +
	"Categories: quality (tool results), pricing (charges or prices), friction (using treg), " +
	"other (requests or suggestions). Pass related call IDs in call_ids (CLI: --call-id), " +
	"not only in message. References are optional. Omit private information, credentials, " +
	"and raw requests, responses or logs. Report each issue once. Feedback goes to the treg " +
	"team to improve the product; it does not complete the user's task. Keep going afterward."

const feedbackFields = "\n\nSubmission fields:\n" +
	"  category       Required: quality (results), pricing (charges/prices),\n" +
	"                 friction (using treg), other (requests/suggestions).\n" +
	"  message        Required: what you needed and what happened, 1-2000 characters.\n" +
	"                 State uncertainty; use - to read sanitized text from stdin.\n" +
	"  --call-id ID   Optional: the treg call ID returned with the relevant call.\n" +
	"                 Repeat for multiple calls (up to 100); sent as call_ids.\n" +
	"                 IDs written only in message are not linked automatically.\n" +
	"  --endpoint-id ID\n" +
	"                 Optional: public catalog endpoint ID; sent as endpoint_id.\n" +
	"\nReceipt: JSON with feedback_id and status=received. Retrieve with:\n" +
	"  treg feedback get FEEDBACK_ID\n" +
	"Reports go to your configured registry and active team, cost nothing, and\n" +
	"are visible to that team and registry administrators. No automatic reply.\n"

// feedbackCmd holds `submit` and `get`. `treg feedback <category> ...` is the original spelling of
// `treg feedback submit <category> ...`, so the group reads its own arguments to route it.
func feedbackCmd(env *Env) *cobra.Command {
	submit := feedbackSubmitCmd(env)
	cmd := leaf("feedback", "Submit or retrieve private team feedback.",
		`treg feedback submit friction "The pagination example is unclear."`,
		`treg feedback submit quality "The result is outdated." --call-id CALL_ID --endpoint-id PROVIDER.ENDPOINT`,
		`treg feedback submit other - < sanitized-feedback.txt`,
		`treg feedback get 123`)
	cmd.Short = "Share a problem or suggestion about treg."
	cmd.Long = feedbackDescription
	cmd.Example += feedbackFields + "\nFull syntax: treg feedback submit --help"
	cmd.Args = cobra.ArbitraryArgs
	cmd.DisableFlagParsing = true
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		if len(args) == 0 || args[0] == "-h" || args[0] == "--help" {
			return cmd.Help()
		}
		if contains(feedbackCategories, args[0]) {
			if err := submit.Flags().Parse(args); err != nil {
				return err
			}
			if help, _ := submit.Flags().GetBool("help"); help {
				return submit.Help()
			}
			return submit.RunE(submit, submit.Flags().Args())
		}
		return invalidSubcommand(cmd, args[0])
	}
	cmd.AddCommand(submit, feedbackGetCmd(env))
	return cmd
}

func feedbackSubmitCmd(env *Env) *cobra.Command {
	var callIDs []string
	var endpointID string
	cmd := leaf("submit <category> <message>", "Submit a problem or suggestion.",
		`treg feedback submit friction "The pagination example is unclear."`,
		`treg feedback submit quality "The returned data is outdated." --call-id CALL_ID`,
		`treg feedback submit other - < sanitized-feedback.txt`)
	cmd.Long = feedbackDescription
	cmd.Example += feedbackFields + "\nMore: <your registry base URL>/feedback.md"
	cmd.Args = cobra.ExactArgs(2)
	cmd.Flags().StringArrayVar(&callIDs, "call-id", nil, "returned treg call ID; repeat up to 100; sent as call_ids, not extracted from message")
	cmd.Flags().StringVar(&endpointID, "endpoint-id", "", "public catalog endpoint ID, if known")
	cmd.InitDefaultHelpFlag()
	cmd.RunE = func(_ *cobra.Command, args []string) error {
		if len(args) != 2 {
			return fmt.Errorf("the following arguments are required: category, message")
		}
		if err := choice("category", args[0], feedbackCategories...); err != nil {
			return err
		}
		message := args[1]
		if message == "-" {
			if term.IsTerminal(os.Stdin) {
				return feedbackError("stdin_required", "Pipe sanitized text or redirect a file into stdin when using '-'.", nil)
			}
			raw, _ := io.ReadAll(os.Stdin)
			message = string(raw)
		}
		length := utf8.RuneCountInString(providers.Strip(message))
		if length < 1 || length > 2000 {
			return feedbackError("invalid_message", "Message must contain 1-2000 characters after trimming. "+
				"Edit the description and submit again.", pyjson.Ordered("actual_length", length, "max_length", 2000))
		}
		b := pyjson.Ordered("category", args[0], "message", message)
		if len(callIDs) > 0 {
			b.Set("call_ids", callIDs)
		}
		if endpointID != "" {
			b.Set("endpoint_id", endpointID)
		}
		return env.feedbackRequest("POST", "/feedback", b)
	}
	return cmd
}

// feedbackError is a refusal an agent can parse: one JSON line on stdout, then status 1.
func feedbackError(code, message string, details *pyjson.Object) error {
	o := pyjson.Ordered("error", code, "message", message)
	for _, k := range details.Keys() {
		o.Set(k, details.Value(k))
	}
	outln(pyjson.Dumps(o, pyjson.Line))
	return &output.Exit{Code: 1}
}

// feedbackRequest sends a feedback submission or lookup and prints the receipt. A refusal is
// mapped to a fixed message by status, because a validation answer can echo the rejected input.
func (e *Env) feedbackRequest(method, path string, b *pyjson.Object) error {
	submitting := method == "POST"
	uncertain := "Could not confirm whether feedback was saved. Check connectivity before submitting again."
	req := api.Request{Method: method, Path: path}
	if b != nil {
		req.Body, req.Header = jsonBody(b), jsonHeader
	}
	r, err := e.Client().Do(req)
	if err != nil {
		if submitting {
			return feedbackError("submission_unconfirmed", uncertain, nil)
		}
		return feedbackError("request_failed", "Could not retrieve feedback. Check connectivity and retry.", nil)
	}
	if r.Status >= 400 {
		code, message := "request_failed", "Could not retrieve feedback. Try again later."
		if submitting {
			code, message = "submission_unconfirmed", uncertain
		}
		switch r.Status {
		case 401:
			code, message = "authentication_required", "Sign in with `treg login`, or check the configured token."
		case 403:
			code, message = "access_denied", "Check the active team and your token's permissions."
		case 404:
			code, message = "not_found", "No feedback is available with this ID in the active team. Check the ID and team."
		case 422:
			code, message = "invalid_feedback", "Check the fields with `treg feedback submit --help`. "+
				"Messages must contain 1-2000 characters; at most 100 call IDs are allowed."
		case 429:
			code, message = "rate_limited", "This team has reached its feedback submission limit. Try again later."
		}
		return feedbackError(code, message, pyjson.Ordered("http_status", r.Status))
	}
	receipt, err := pyjson.Decode(r.Body)
	if err != nil {
		if submitting {
			return feedbackError("invalid_response", uncertain, nil)
		}
		return feedbackError("invalid_response", "Invalid response. Retry the lookup later.", nil)
	}
	outln(pyjson.Dumps(receipt, pyjson.Pretty))
	return nil
}
