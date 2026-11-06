package agents

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/BurntSushi/toml"

	"github.com/superdesigndev/treg/cli/internal/pyjson"
)

// MCP status values.
const (
	OK      = "ok"
	Skipped = "skipped"
	Failed  = "error"
)

// Result is one agent's outcome: its display name, a status and a detail line.
type Result struct {
	Display, Status, Detail string
}

// Manual is an agent treg detects but does not write, with how to add the server by hand.
type Manual struct {
	Display, How string
}

type Installed struct {
	Results []Result
	Manual  []Manual
	URL     string
}

type mcpAgent struct {
	display string
	marker  func() bool
	write   func(name, url, token string, oauth bool) (string, string)
}

func onPath(bin string) bool {
	_, err := exec.LookPath(bin)
	return err == nil
}

// MCP configs live at fixed home-relative paths whatever CLAUDE_CONFIG_DIR or CODEX_HOME say: the
// clients read their server lists from there.
func mcpAgents() []mcpAgent {
	h := home()
	cursor := filepath.Join(h, ".cursor", "mcp.json")
	codex := filepath.Join(h, ".codex", "config.toml")
	opencode := filepath.Join(configHome(), "opencode", "opencode.json")
	return []mcpAgent{
		{"Claude Code", func() bool { return exists(filepath.Join(h, ".claude")) || onPath("claude") }, writeClaude},
		{"Cursor", func() bool { return exists(filepath.Join(h, ".cursor")) }, func(name, url, token string, oauth bool) (string, string) {
			return writeJSONAgent(cursor, "mcpServers", name, pyjson.Ordered("url", url, "headers", bearer(token)), oauth)
		}},
		{"Codex", func() bool { return exists(filepath.Join(h, ".codex")) || onPath("codex") }, func(name, url, token string, oauth bool) (string, string) {
			return writeCodex(codex, name, url, token, oauth)
		}},
		{"opencode", func() bool { return exists(filepath.Join(configHome(), "opencode")) }, func(name, url, token string, oauth bool) (string, string) {
			entry := pyjson.Ordered("type", "remote", "url", url, "enabled", true, "headers", bearer(token))
			return writeJSONAgent(opencode, "mcp", name, entry, oauth)
		}},
	}
}

func bearer(token string) *pyjson.Object {
	return pyjson.Ordered("Authorization", "Bearer "+token)
}

var manualAgents = []struct {
	display string
	marker  func() bool
	how     string
}{
	{"Hermes", func() bool { return exists(filepath.Join(home(), ".hermes")) },
		"add to ~/.hermes/config.yaml under mcp_servers: a `url` + `headers: " +
			"{Authorization: \"Bearer <token>\"}` entry."},
	{"DeepSeek Harness", func() bool { return exists(filepath.Join(home(), ".dsh")) || onPath("dsh") },
		"dsh has no MCP config file to write - a server is a row in a PROFILE's config " +
			"layer. Install the bundle instead, which carries that row: `dsh plugin " +
			"--profile <name> add github:superdesigndev/treg`. The row stays disabled until " +
			"TREG_TOKEN is in the environment dsh starts in, so export it and restart dsh."},
	{"OpenClaw", func() bool { return exists(filepath.Join(home(), ".openclaw")) || onPath("openclaw") },
		"run `openclaw mcp add --transport http treg <url> --header " +
			"\"Authorization: Bearer <token>\"`."},
}

const oauthManualHow = "treg does not write this agent's config. If its treg entry carries an " +
	"Authorization header, replace that key with a current team key and restart it."

// InstallMCP registers treg's MCP server, <base>/mcp/, in every supported agent installed here.
// With oauth the entries carry the URL only, so each client authorizes itself; token is unused.
func InstallMCP(base, token, name string, oauth bool) Installed {
	url := strings.TrimRight(base, "/") + "/mcp/"
	out := Installed{URL: url}
	for _, a := range mcpAgents() {
		if !a.marker() {
			continue
		}
		status, detail := a.write(name, url, token, oauth)
		out.Results = append(out.Results, Result{a.display, status, detail})
	}
	for _, m := range manualAgents {
		if !m.marker() {
			continue
		}
		how := m.how
		if oauth {
			how = oauthManualHow
		}
		out.Manual = append(out.Manual, Manual{m.display, how})
	}
	return out
}

func writeClaude(name, url, token string, oauth bool) (string, string) {
	claude, err := exec.LookPath("claude")
	if err != nil {
		return Skipped, "the `claude` CLI is not on PATH"
	}
	// `--scope user` makes the server available in every project, not only the current directory.
	var exit *exec.ExitError
	if _, _, err := runFor(30*time.Second, claude, "mcp", "remove", "--scope", "user", name); err != nil && !errors.As(err, &exit) {
		if errors.Is(err, context.DeadlineExceeded) {
			return Failed, "`claude mcp remove` did not finish within 30 seconds"
		}
		return Failed, err.Error()
	}
	args := []string{"mcp", "add", "--scope", "user", "--transport", "http", name, url}
	if !oauth {
		args = append(args, "--header", "Authorization: Bearer "+token)
	}
	stdout, stderr, err := runFor(30*time.Second, claude, args...)
	switch {
	case errors.As(err, &exit):
		msg := stderr
		if msg == "" {
			msg = stdout
		}
		if msg == "" {
			msg = "claude mcp add failed"
		}
		return Failed, clip(strings.TrimSpace(msg), 200)
	case errors.Is(err, context.DeadlineExceeded):
		return Failed, "`claude mcp add` did not finish within 30 seconds"
	case err != nil:
		return Failed, err.Error()
	}
	if oauth {
		return OK, "via `claude mcp add --scope user` (URL only; authorize in `/mcp`)"
	}
	return OK, "via `claude mcp add --scope user`"
}

func runFor(limit time.Duration, bin string, args ...string) (string, string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), limit)
	defer cancel()
	cmd := exec.CommandContext(ctx, bin, args...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	err := cmd.Run()
	if ctx.Err() != nil {
		err = ctx.Err()
	}
	return stdout.String(), stderr.String(), err
}

func clip(s string, n int) string {
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	return string([]rune(s)[:n])
}

// writeJSONAgent merges one server entry into a JSON config, keeping everything else as it was.
func writeJSONAgent(path, root, name string, entry *pyjson.Object, oauth bool) (string, string) {
	data := pyjson.NewObject()
	if exists(path) {
		raw, err := ReadText(path)
		if err != nil {
			return Failed, fmt.Sprintf("%s: %v", path, err)
		}
		v, err := pyjson.Decode([]byte(raw))
		if err != nil {
			return Failed, fmt.Sprintf("%s: %v", path, err)
		}
		obj, ok := v.(*pyjson.Object)
		if !ok {
			return Failed, fmt.Sprintf("%s is not a JSON object", path)
		}
		data = obj
	}
	servers := pyjson.NewObject()
	if v, ok := data.Get(root); ok {
		if obj, ok := v.(*pyjson.Object); ok {
			servers = obj
		}
	}
	if oauth {
		entry.Delete("headers")
	}
	servers.Set(name, entry)
	data.Set(root, servers)
	text := pyjson.Dumps(data, pyjson.Options{Indent: 2, EnsureASCII: true}) + "\n"
	if err := WritePrivate(path, text); err != nil {
		return Failed, fmt.Sprintf("%s: %v", path, err)
	}
	return OK, path
}

// writeCodex writes one `[mcp_servers.<name>]` table. Any earlier table of that name, or of
// another name pointing at the same URL, is cut out as text and a fresh table appended; the result
// must parse and hold exactly this entry, or nothing is written. Codex reads a literal token from
// http_headers; a token named by environment variable is lost when the Codex app restarts.
func writeCodex(path, name, url, token string, oauth bool) (string, string) {
	fail := func(err error) (string, string) { return Failed, fmt.Sprintf("%s: %v", path, err) }
	old := ""
	if exists(path) {
		raw, err := ReadText(path)
		if err != nil {
			return fail(err)
		}
		old = raw
	}
	var parsed map[string]any
	if _, err := toml.Decode(old, &parsed); err != nil {
		return fail(err)
	}
	stale := []string{name}
	if servers, ok := parsed["mcp_servers"]; ok {
		table, ok := servers.(map[string]any)
		if !ok {
			return fail(errors.New("mcp_servers is not a table"))
		}
		for k, v := range table {
			if t, ok := v.(map[string]any); ok && t["url"] == url && k != name {
				stale = append(stale, k)
			}
		}
	}
	kept := old
	for _, k := range stale {
		kept = cutTable(kept, k)
	}
	kept = strings.TrimRightFunc(kept, isPySpace)
	header := "[mcp_servers." + name + "]"
	block := header + "\nurl = " + pyjson.Quote(url, true) + "\n"
	expected := map[string]any{"url": url}
	if !oauth {
		block += "http_headers = { \"Authorization\" = " + pyjson.Quote("Bearer "+token, true) + " }\n"
		expected["http_headers"] = map[string]any{"Authorization": "Bearer " + token}
	}
	text := block
	if kept != "" {
		text = kept + "\n\n" + block
	}
	var check map[string]any
	if _, err := toml.Decode(text, &check); err != nil {
		return fail(err)
	}
	servers, _ := check["mcp_servers"].(map[string]any)
	if !sameTable(servers[name], expected) || twinOf(servers, name, url) {
		return Failed, fmt.Sprintf("%s: a sub-table of %s survived; edit it by hand", path, header)
	}
	if err := WritePrivate(path, text); err != nil {
		return fail(err)
	}
	return OK, path
}

// cutTable removes every `[mcp_servers.<k>]` table: its header line through the line before the
// next line that opens a table, or the end.
func cutTable(text, k string) string {
	header := "[mcp_servers." + k + "]\n"
	var out strings.Builder
	i := 0
	for {
		pos := -1
		for from := i; from <= len(text); {
			j := strings.Index(text[from:], header)
			if j < 0 {
				break
			}
			at := from + j
			if at == 0 || text[at-1] == '\n' {
				pos = at
				break
			}
			from = at + 1
		}
		if pos < 0 {
			out.WriteString(text[i:])
			return out.String()
		}
		out.WriteString(text[i:pos])
		end := pos + len(header)
		for end < len(text) && !(text[end] == '[' && text[end-1] == '\n') {
			end++
		}
		i = end
	}
}

func sameTable(got any, want map[string]any) bool {
	g, ok := got.(map[string]any)
	if !ok || len(g) != len(want) {
		return false
	}
	for k, w := range want {
		switch wv := w.(type) {
		case map[string]any:
			if !sameTable(g[k], wv) {
				return false
			}
		default:
			if g[k] != wv {
				return false
			}
		}
	}
	return true
}

func twinOf(servers map[string]any, name, url string) bool {
	for k, v := range servers {
		if t, ok := v.(map[string]any); ok && k != name && t["url"] == url {
			return true
		}
	}
	return false
}

// isPySpace is Python's str.isspace for one character.
func isPySpace(r rune) bool {
	switch r {
	case ' ', '\t', '\n', '\v', '\f', '\r', 0x1c, 0x1d, 0x1e, 0x1f, 0x85, 0xa0, 0x1680, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000:
		return true
	}
	return r >= 0x2000 && r <= 0x200a
}

// WritePrivate replaces path atomically with text, mode 0600 before any byte is written: the file
// holds a bearer token. On Windows the file inherits the config directory's ACL.
func WritePrivate(path, text string) error {
	dir := filepath.Dir(path)
	if err := MkdirLeaf(dir, 0o700); err != nil {
		return err
	}
	f, err := os.CreateTemp(dir, "."+filepath.Base(path)+".*.tmp")
	if err != nil {
		return err
	}
	tmp := f.Name()
	ok := false
	defer func() {
		if !ok {
			f.Close()
			os.Remove(tmp)
		}
	}()
	if runtime.GOOS != "windows" {
		if err := f.Chmod(0o600); err != nil {
			return err
		}
	}
	if _, err := f.WriteString(text); err != nil {
		return err
	}
	if err := f.Sync(); err != nil {
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		return err
	}
	ok = true
	return nil
}

// Wrap is textwrap.wrap for prose without hyphenated words.
func Wrap(text string, width int) []string {
	var lines []string
	line := ""
	for _, word := range strings.Fields(text) {
		switch {
		case line == "":
			line = word
		case utf8.RuneCountInString(line)+1+utf8.RuneCountInString(word) <= width:
			line += " " + word
		default:
			lines = append(lines, line)
			line = word
		}
	}
	if line != "" {
		lines = append(lines, line)
	}
	return lines
}
