package commands

import (
	"fmt"
	"strconv"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/output"
)

func feedbackCmd(env *Env) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "feedback",
		Short: "Submit or retrieve private team feedback.",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return cmd.Help()
		},
	}
	cmd.AddCommand(feedbackGetCmd(env))
	return cmd
}

func feedbackGetCmd(env *Env) *cobra.Command {
	return &cobra.Command{
		Use:     "get <feedback_id>",
		Short:   "Retrieve a feedback report from the active team.",
		Example: "  treg feedback get 123",
		Args:    cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			id, err := strconv.Atoi(args[0])
			if err != nil {
				return fmt.Errorf("argument feedback_id: invalid int value: %s", pyRepr(args[0]))
			}
			return env.feedbackGet(id)
		},
	}
}

// feedbackGet prints the report, or a refusal that never repeats what the registry answered:
// the same JSON in both output modes.
func (e *Env) feedbackGet(id int) error {
	if id < 1 {
		return feedbackGetError("invalid_id", "Feedback ID must be a positive integer from a submission receipt.")
	}
	resp, err := e.Client().Do(api.Request{Path: fmt.Sprintf("/feedback/%d", id), Header: [][2]string{api.WithView}})
	if err != nil {
		return feedbackGetError("request_failed", "Could not retrieve feedback. Check connectivity and retry.")
	}
	v, ok := resp.View()
	if !ok {
		return e.Out.ShowUnrendered(resp)
	}
	outf("%s", v.Text)
	if resp.Status >= 400 {
		return &output.Exit{Code: 1}
	}
	return nil
}

func feedbackGetError(code, message string) error {
	outln(pyDumps(ordered{{"error", code}, {"message", message}}))
	return &output.Exit{Code: 1}
}
