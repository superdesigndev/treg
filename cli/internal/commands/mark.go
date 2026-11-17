package commands

import (
	"regexp"
	"slices"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/ojson"
	"github.com/superdesigndev/treg/cli/internal/providers"
)

var (
	markBases    = []string{"bounced", "cross_checked", "user_said", "impression"}
	markVerdicts = []string{"correct", "wrong", "stale", "wrong_entity", "missing", "irrelevant", "duplicate", "other"}
	markField    = regexp.MustCompile(`^[A-Za-z0-9_.\[\]-]{1,120}$`)
)

const markDescription = "Mark what you found wrong, or confirmed right, in a call's result once you know: " +
	"an email bounced, another source disagreed, or the user corrected it. Each item is " +
	"[INDEX][/FIELD]:VERDICT, INDEX the 0-based row; a bare VERDICT is the whole result. Say how you know " +
	"with --basis. Rows you did not mark carry no conclusion. Marks are private and never change charges. " +
	"Omit private information, then keep going with the task."

func markCmd(env *Env) *cobra.Command {
	var basis, evidence, note string
	var checked, replaces int
	cmd := leaf("mark <call_id> [item...]", markDescription,
		"treg mark CALL_ID 2/email:wrong --basis bounced --checked 10\n"+
			"treg mark CALL_ID correct --basis cross_checked --evidence VERIFY_CALL_ID")
	cmd.Short = "Mark wrong or confirmed rows in a call's result."
	cmd.Args = cobra.MinimumNArgs(1)
	cmd.Flags().StringVar(&basis, "basis", "", "how you know: "+strings.Join(markBases, ", "))
	cmd.Flags().IntVar(&checked, "checked", 0, "how many rows you looked at")
	cmd.Flags().StringVar(&evidence, "evidence", "", "the call ID that showed it, such as a verify call")
	cmd.Flags().StringVar(&note, "note", "", "optional private note, 1-500 characters")
	cmd.Flags().IntVar(&replaces, "replaces", 0, "an earlier mark ID this one corrects; no items withdraws it")
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		if err := choice("basis", basis, markBases...); err != nil {
			return err
		}
		callID := providers.Strip(args[0])
		if !callIDShape.MatchString(callID) {
			return feedbackError("invalid_call_id", "Use the call ID from the catalog call response.", nil)
		}
		items := make([]any, 0, len(args)-1)
		for _, raw := range args[1:] {
			item, ok := parseMarkItem(raw)
			if !ok {
				return feedbackError("invalid_item", "Write each item as [INDEX][/FIELD]:VERDICT, for example 2/email:wrong, "+
					"with a verdict from: "+strings.Join(markVerdicts, ", ")+".", ojson.Ordered("item", raw))
			}
			items = append(items, item)
		}
		changed := cmd.Flags().Changed
		if len(items) == 0 && !changed("replaces") {
			return feedbackError("invalid_mark", "Name at least one item, or withdraw a mark with --replaces and no items.", nil)
		}
		b := ojson.Ordered("call_id", callID, "basis", basis, "items", items)
		if changed("checked") {
			if checked < 0 {
				return feedbackError("invalid_mark", "--checked cannot be negative.", nil)
			}
			b.Set("checked", checked)
		}
		if changed("evidence") {
			evidence = providers.Strip(evidence)
			if !callIDShape.MatchString(evidence) {
				return feedbackError("invalid_call_id", "--evidence takes a call ID from a call response.", nil)
			}
			b.Set("evidence_call_id", evidence)
		}
		if changed("note") {
			note = providers.Strip(note)
			if n := utf8.RuneCountInString(note); n < 1 || n > 500 {
				return feedbackError("invalid_note", "Note must contain 1-500 characters after trimming. Omit private data.", nil)
			}
			b.Set("note", note)
		}
		if changed("replaces") {
			if replaces < 1 {
				return feedbackError("invalid_mark", "--replaces takes a mark ID from an earlier receipt.", nil)
			}
			b.Set("replaces", replaces)
		}
		uncertain := "Could not confirm whether the mark was saved. Retry the same command to confirm."
		r, err := env.Client().Do(api.Request{Method: "POST", Path: "/marks", Body: ojson.Compact(b), Header: jsonHeader})
		if err != nil {
			return feedbackError("submission_unconfirmed", uncertain, nil)
		}
		if r.Status >= 400 {
			code, message := "submission_unconfirmed", uncertain
			switch r.Status {
			case 400:
				code, message = "not_catalog_call", "Marks require a catalog call, not a team's own tool."
			case 401:
				code, message = "authentication_required", "Sign in with `treg login`, or check the configured token."
			case 403:
				code, message = "access_denied", "Check the active team and your token's permissions."
			case 404:
				code, message = "not_found", "Call or replaced mark not found in the active team; a new call may not be written yet. Retry shortly."
			case 422:
				code, message = "invalid_mark", "Check the fields with `treg mark --help`. Omit private data."
			case 429:
				code, message = "rate_limited", "Too many marks this hour. Keep going with the task and retry later."
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

// parseMarkItem reads [INDEX][/FIELD]:VERDICT, or FIELD:VERDICT, or a bare VERDICT.
func parseMarkItem(raw string) (*ojson.Object, bool) {
	raw = strings.TrimSpace(raw)
	where, verdict, found := strings.Cut(raw, ":")
	if !found {
		where, verdict = "", raw
	}
	if !isMarkVerdict(verdict) {
		return nil, false
	}
	item := ojson.Ordered("verdict", verdict)
	if where == "" {
		return item, !found
	}
	index, field, hasField := strings.Cut(where, "/")
	if !hasField {
		if _, err := strconv.Atoi(where); err != nil {
			index, field, hasField = "", where, true
		}
	}
	if index != "" {
		n, err := strconv.Atoi(index)
		if err != nil || n < 0 {
			return nil, false
		}
		item.Set("index", n)
	}
	if hasField {
		if !markField.MatchString(field) {
			return nil, false
		}
		item.Set("field", field)
	}
	return item, true
}

func isMarkVerdict(v string) bool {
	return slices.Contains(markVerdicts, v)
}
