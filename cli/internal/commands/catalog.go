package commands

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/output"
)

const catalogUsage = `Browse the library of endpoints & tools for data access and integrations.

Usage:
  treg catalog [<platform|search|get|request>] [<args>...] [flags]

Flags:
  --limit N       search: how many results (default: 25, max 100); get: how many providers to compare (default: 10)
  --new [DAYS]    search: only tools added in the last DAYS days (default 30, max 365); words are optional
  --sort SORT     search: best match (best, the default) or newest first (newest)
  --all           include management endpoints (account/utility CRUD) hidden from the browse by default

Examples:
  treg catalog                                   # platforms, busiest first
  treg catalog tiktok                            # every provider's tiktok endpoints, by capability
  treg catalog search tiktok comments            # find an endpoint by what it does
  treg catalog search --new                      # tools added in the last 30 days, newest first
  treg catalog search phone --new 60             # phone tools added in the last 60 days
  treg catalog search linkedin --sort newest     # linkedin tools, newest first
  treg catalog get tikhub.tiktok.video.comments  # params, cost, example response, how to call it
  treg catalog request "Ahrefs backlinks"        # missing? file it - requests steer what gets added
`

type catalogArgs struct {
	verb  string
	rest  []string
	limit *int
	days  *int
	sort  string
	all   bool
}

func catalogCmd(env *Env) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "catalog [<platform|search|get|request>] [<args>...]",
		Short: "Browse the library of endpoints & tools for data access and integrations.",
		// `search`, `get` and `request` are words, not subcommands, so `treg catalog <platform>`
		// keeps working and a query needs no quoting; `--new` takes an optional number.
		DisableFlagParsing: true,
		RunE: func(cmd *cobra.Command, argv []string) error {
			a, help, err := parseCatalogArgs(argv)
			if err != nil {
				return err
			}
			if help {
				outf("%s", catalogUsage)
				return nil
			}
			return env.catalog(a)
		},
	}
	return cmd
}

// parseCatalogArgs reads the arguments as argparse did: `--new` consumes the next word only when
// it is not an option, so `--new 60` is a number of days and `--new phone` is a mistake.
func parseCatalogArgs(argv []string) (catalogArgs, bool, error) {
	a := catalogArgs{sort: "best"}
	var words []string
	for i := 0; i < len(argv); i++ {
		arg := argv[i]
		name, value, inline := strings.Cut(arg, "=")
		next := func() (string, bool) {
			if inline {
				return value, true
			}
			if i+1 < len(argv) && !isOption(argv[i+1]) {
				i++
				return argv[i], true
			}
			return "", false
		}
		switch {
		case arg == "--":
			words = append(words, argv[i+1:]...)
			i = len(argv)
		case arg == "-h" || arg == "--help":
			return a, true, nil
		case name == "--limit":
			v, ok := next()
			if !ok {
				return a, false, fmt.Errorf("argument --limit: expected one argument")
			}
			n, err := strconv.Atoi(v)
			if err != nil {
				return a, false, fmt.Errorf("argument --limit: invalid int value: %s", quote(v))
			}
			a.limit = &n
		case name == "--new":
			days := 30
			if v, ok := next(); ok {
				n, err := strconv.Atoi(v)
				if err != nil {
					return a, false, fmt.Errorf("argument --new: --new takes a number of days, not %s; put search "+
						"words before --new (treg catalog search phone --new 60)", quote(v))
				}
				days = n
			}
			a.days = &days
		case name == "--sort":
			v, ok := next()
			if !ok {
				return a, false, fmt.Errorf("argument --sort: expected one argument")
			}
			if v != "best" && v != "newest" {
				return a, false, fmt.Errorf("argument --sort: invalid choice: %s (choose from best, newest)", quote(v))
			}
			a.sort = v
		case arg == "--all":
			a.all = true
		case isOption(arg):
			return a, false, fmt.Errorf("unrecognized arguments: %s", arg)
		default:
			words = append(words, arg)
		}
	}
	if len(words) > 0 {
		a.verb, a.rest = words[0], words[1:]
	}
	return a, false, nil
}

// isOption is a word argparse reads as an option: a dash that does not start a negative number.
func isOption(s string) bool {
	if !strings.HasPrefix(s, "-") || s == "-" {
		return false
	}
	_, err := strconv.ParseFloat(s, 64)
	return err != nil
}

func (e *Env) catalog(a catalogArgs) error {
	switch a.verb {
	case "search":
		e.Note("command", "catalog_search")
		return e.catalogSearch(strings.Join(a.rest, " "), a)
	case "get":
		e.Note("command", "catalog_get")
		if len(a.rest) == 0 {
			return output.Fail("which endpoint? e.g. treg catalog get tikhub.tiktok.video.comments\n" +
				"find one with: treg catalog search <query>")
		}
		e.noteTool(a.rest[0])
		return e.catalogGet(a.rest[0], a.limit)
	case "request":
		e.Note("command", "catalog_request")
		return e.catalogRequest(strings.Join(a.rest, " "))
	}
	if a.verb != "" {
		e.Note("command", "catalog_platform")
	}
	if len(a.rest) > 0 {
		return output.Fail("unexpected argument %s - did you mean `treg catalog search %s %s`?",
			quote(a.rest[0]), a.verb, strings.Join(a.rest, " "))
	}
	if a.verb == "" {
		return e.catalogPage(e.AnonymousClient(), api.Request{Path: "/catalog/platforms"}, "")
	}
	r := api.Request{Path: "/catalog/platforms/" + quoteAll(a.verb)}
	if a.all {
		r.Query = [][2]string{{"include_hidden", "1"}}
	}
	return e.catalogPage(e.Client(), r, "")
}

func (e *Env) catalogSearch(query string, a catalogArgs) error {
	added := a.days != nil || a.sort == "newest"
	if strings.TrimSpace(query) == "" && !added {
		return output.Fail("search for what? e.g. treg catalog search tiktok comments\n" +
			"or list new tools: treg catalog search --new")
	}
	limit := 25
	if a.limit != nil && *a.limit != 0 {
		limit = *a.limit
	}
	q := [][2]string{{"q", query}, {"limit", strconv.Itoa(limit)}}
	if a.days != nil {
		q = append(q, [2]string{"added_within_days", strconv.Itoa(*a.days)})
	}
	if a.sort != "best" {
		q = append(q, [2]string{"sort", a.sort})
	}
	if !added {
		q = append(q, [2]string{"engine", "agent"})
	}
	return e.catalogPage(e.Client(), api.Request{Path: "/catalog/search", Query: q}, "")
}

func (e *Env) catalogGet(id string, limit *int) error {
	r := api.Request{Path: "/catalog/endpoints/" + quoteAll(id)}
	if limit != nil {
		r.Query = [][2]string{{"limit", strconv.Itoa(*limit)}}
	}
	return e.catalogPage(e.Client(), r, id)
}

// catalogPage prints a catalog view. `id` is the one `catalog get` asked for: a job id answers
// as a job, and an id that is neither is the refusal `unknown_endpoint`.
func (e *Env) catalogPage(c *api.Client, r api.Request, id string) error {
	r.Header = append(r.Header, api.WithView)
	resp, err := e.do(c, r)
	if err != nil {
		return err
	}
	v, ok := resp.View()
	if !ok {
		return e.Out.ShowUnrendered(resp)
	}
	if id != "" && resp.Status == 200 && answersAJob(id, v.Data) {
		e.Note("command", "catalog_job")
	}
	if e.JSON {
		return e.Out.ShowRaw(resp, v)
	}
	outf("%s", v.Text)
	if resp.Status < 400 {
		return nil
	}
	if id != "" && resp.Status == 404 {
		e.Note("error", "unknown_endpoint")
	} else {
		e.Note("error", fmt.Sprintf("http_%d", resp.Status))
	}
	return &output.Exit{Code: 1}
}

// answersAJob: `catalog get <job>` answers with the job's routed endpoint, or the job itself.
func answersAJob(id string, data []byte) bool {
	var page struct {
		Job      *string `json:"job"`
		Endpoint *struct {
			ID string `json:"id"`
		} `json:"endpoint"`
	}
	if json.Unmarshal(data, &page) != nil {
		return false
	}
	return page.Job != nil || (page.Endpoint != nil && page.Endpoint.ID != id)
}

func (e *Env) catalogRequest(text string) error {
	capability := strings.TrimSpace(text)
	if capability == "" {
		return output.Fail(`request what? e.g. treg catalog request "Ahrefs backlinks"`)
	}
	body, _ := marshalCompact(ordered{{"capability", capability}, {"source", "cli"}})
	resp, err := e.do(e.Client(), api.Request{Method: "POST", Path: "/tool-requests", Body: body,
		Header: [][2]string{{"Content-Type", "application/json"}, api.WithView}})
	if err != nil {
		return err
	}
	v, ok := resp.View()
	if !ok {
		return e.Out.ShowUnrendered(resp)
	}
	if e.JSON {
		return e.Out.Show(output.Data(resp, v))
	}
	return e.Out.ShowView(resp, v)
}

// quoteAll is Python's urllib.parse.quote(s, safe=""): only unreserved characters stay bare.
func quoteAll(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		if 'A' <= c && c <= 'Z' || 'a' <= c && c <= 'z' || '0' <= c && c <= '9' || strings.IndexByte("-._~", c) >= 0 {
			b.WriteByte(c)
		} else {
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

// marshalCompact is JSON without spaces and without HTML escaping, as httpx sends a body.
func marshalCompact(v any) ([]byte, error) {
	data, err := output.MarshalIndent(v)
	if err != nil {
		return nil, err
	}
	var buf bytes.Buffer
	if err := json.Compact(&buf, data); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}
