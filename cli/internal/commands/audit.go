package commands

import (
	"encoding/json"
	"sort"
	"strings"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/providers"
	"github.com/superdesigndev/treg/cli/internal/pyjson"
)

func auditCmd(env *Env) *cobra.Command {
	var limit string
	var calls, runs bool
	cmd := leaf("audit", "Who called/ran what, when, and the result - proxy calls and CLI runs, one log.",
		"treg audit --limit 20", "treg audit --calls          # only proxy calls",
		"treg audit --runs           # only CLI runs (server + local)")
	cmd.Short = "Who called/ran what, when, and the result."
	cmd.Args = cobra.NoArgs
	fl := cmd.Flags()
	fl.StringVar(&limit, "limit", "50", "how many recent rows")
	fl.BoolVar(&calls, "calls", false, "only proxy calls (what `treg calls` showed)")
	fl.BoolVar(&runs, "runs", false, "only CLI runs, both tiers (what `treg runs` showed)")
	cmd.MarkFlagsMutuallyExclusive("calls", "runs")
	cmd.RunE = func(*cobra.Command, []string) error {
		n, err := intArg("--limit", limit)
		if err != nil {
			return err
		}
		switch {
		case calls:
			return env.show(api.Request{Path: "/calls", Query: [][2]string{{"limit", string(n)}}})
		case runs:
			return env.runs(n)
		}
		return env.audit(n)
	}
	return cmd
}

// callsCmd is the hidden back-compat spelling of `treg audit --calls`.
func callsCmd(env *Env) *cobra.Command {
	var limit string
	cmd := leaf("calls", "(alias) the proxy-call half of `treg audit`.")
	cmd.Hidden = true
	cmd.Args = cobra.NoArgs
	cmd.Flags().StringVar(&limit, "limit", "50", "how many recent calls")
	cmd.RunE = func(*cobra.Command, []string) error {
		n, err := intArg("--limit", limit)
		if err != nil {
			return err
		}
		return env.show(api.Request{Path: "/calls", Query: [][2]string{{"limit", string(n)}}})
	}
	return cmd
}

func (e *Env) runs(limit json.Number) error {
	r, err := e.do(e.Client(), api.Request{Path: "/runs", Query: [][2]string{{"limit", string(limit)}}})
	if err != nil {
		return err
	}
	// A local run reports back only on failure, so a null exit_code is a completed run.
	if r.Status == 200 {
		for _, row := range responseList(r) {
			o, _ := row.(*pyjson.Object)
			if o.Value("where") == "local" && o.Value("exit_code") == nil {
				errln("note: local runs report back only on failure - exit_code null = completed, no report")
				break
			}
		}
	}
	return e.Out.Show(r)
}

// audit merges /calls and /runs, newest first. /runs already lists local runs, so the call
// records of kind local_run are dropped rather than shown twice.
func (e *Env) audit(limit json.Number) error {
	c := e.Client()
	query := [][2]string{{"limit", string(limit)}}
	rc, err := e.do(c, api.Request{Path: "/calls", Query: query})
	if err != nil {
		return err
	}
	rr, err := e.do(c, api.Request{Path: "/runs", Query: query})
	if err != nil {
		return err
	}
	for _, r := range []*api.Response{rc, rr} {
		if r.Status >= 400 {
			return e.Out.Show(r)
		}
	}
	var rows []*pyjson.Object
	for _, item := range responseList(rc) {
		x, _ := item.(*pyjson.Object)
		if x.Value("kind") == "local_run" {
			continue
		}
		row := pyjson.Ordered("kind", "call", "id", "c"+pyjson.Text(x.Value("id")), "user_email", x.Value("user_email"),
			"tool", x.Value("tool_name"),
			"detail", providers.Strip(pyjson.Text(getOr(x, "method", ""))+" "+pyjson.Text(getOr(x, "path", ""))),
			"result", x.Value("status_code"), "where", "proxy", "created_at", x.Value("created_at"))
		if task, ok := x.Value("async_task").(*pyjson.Object); ok && task.Len() > 0 {
			t := pyjson.NewObject()
			for _, k := range []string{"status", "settled_micro", "result_url", "fetch_command", "ttl_note"} {
				t.Set(k, task.Value(k))
			}
			row.Set("task", t)
		}
		rows = append(rows, row)
	}
	for _, item := range responseList(rr) {
		x, _ := item.(*pyjson.Object)
		var argv []string
		if list, ok := x.Value("argv").([]any); ok {
			for _, a := range list {
				argv = append(argv, pyjson.Text(a))
			}
		}
		rows = append(rows, pyjson.Ordered("kind", "run", "id", x.Value("id"), "user_email", x.Value("user_email"),
			"tool", x.Value("tool"), "detail", strings.Join(argv, " "), "result", x.Value("exit_code"),
			"where", x.Value("where"), "created_at", x.Value("created_at")))
	}
	createdAt := func(o *pyjson.Object) string {
		s, _ := o.Value("created_at").(string)
		return s
	}
	sort.SliceStable(rows, func(i, j int) bool { return createdAt(rows[i]) > createdAt(rows[j]) })
	out := make([]any, 0, len(rows))
	n, _ := pyIntValue(limit)
	for _, row := range pySlice(rows, n) {
		out = append(out, row)
	}
	outln(pyjson.Dumps(out, pyjson.Pretty))
	return nil
}

// getOr is Python's dict.get(key, fallback): the fallback only when the key is absent.
func getOr(o *pyjson.Object, key string, fallback any) any {
	if v, ok := o.Get(key); ok {
		return v
	}
	return fallback
}
