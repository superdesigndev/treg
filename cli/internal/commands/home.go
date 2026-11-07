package commands

import (
	"bytes"
	"fmt"
	"os"
	"os/exec"
	"regexp"
	"strings"
	"time"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/buildinfo"
	"github.com/superdesigndev/treg/cli/internal/config"
)

const homeAbout = "The tool catalog for your agent. Search and call its tools"

var homeNextSteps = []string{
	`treg catalog search "<task>"   # find a tool for a job, with prices`,
	"treg catalog get <id>   # params, price, a ready treg call line",
	"treg --help   # every command",
}

// home is bare `treg`: this CLI's own line, the registry when it is not the default one, then
// the registry's view of who is signed in. A registry that cannot answer is named, never fatal.
func (e *Env) home() error {
	c := e.Client().WithTimeout(5 * time.Second)
	resp, err := c.Do(api.Request{Path: "/home", Header: [][2]string{api.WithView}})
	if err != nil {
		return e.homeProblem("unavailable", "not reachable: "+transportReason(err))
	}
	v, ok := resp.View()
	if !ok {
		if resp.Status >= 500 {
			return e.homeProblem("registry_error", fmt.Sprintf("HTTP %d from /home", resp.Status))
		}
		if resp.Status == 200 && bytes.Contains(resp.Body, []byte(`"account"`)) {
			return e.Out.ShowUnrendered(resp)
		}
		return e.homeProblem("unavailable", fmt.Sprintf("HTTP %d from /home is not a treg answer", resp.Status))
	}
	if e.JSON {
		_ = e.Out.ShowRaw(resp, v)
		return nil
	}
	problem := ""
	switch {
	case resp.Status == 200 || resp.Status == 401 || resp.Status == 403:
	case resp.Status >= 500:
		problem = fmt.Sprintf("HTTP %d from /home", resp.Status)
	default:
		problem = fmt.Sprintf("HTTP %d from /home is not a treg answer", resp.Status)
	}
	outln(e.homeLines(problem))
	outf("%s", v.Text)
	return nil
}

// homeLines are the lines only this CLI knows: its version and path, and the registry it talks to.
func (e *Env) homeLines(problem string) string {
	lines := "treg: " + toonString(buildinfo.Version+" at "+executable())
	registry := e.Registry()
	if problem != "" {
		lines += "\nregistry: " + toonString(registry+" ("+problem+")")
	} else if strings.TrimRight(registry, "/") != config.ProductionBaseURL {
		lines += "\nregistry: " + toonString(registry)
	}
	return lines
}

// homeProblem is the home view when the registry could not render one: `kind` is unavailable
// (no treg registry answered) or registry_error (it answered with a server error).
func (e *Env) homeProblem(kind, problem string) error {
	first := "treg config   # which registry this CLI talks to"
	if kind == "registry_error" {
		first = "treg   # again in a minute, the registry answered with a server error"
	}
	help := append([]string{first}, homeNextSteps...)
	if e.JSON {
		outln(pyDumps(ordered{{"bin", executable()}, {"version", buildinfo.Version}, {"description", homeAbout},
			{"registry", e.Registry()}, {kind, problem}, {"help", help}}))
		return nil
	}
	outln(e.homeLines(problem))
	outln("about: " + homeAbout)
	outf("\nhelp[%d]:\n", len(help))
	for _, line := range help {
		outln("  - " + line)
	}
	return nil
}

// executable is how this CLI was started, with the home directory as `~`.
func executable() string {
	exe := os.Args[0]
	if found, err := exec.LookPath(exe); err == nil {
		exe = found
	}
	if home, err := os.UserHomeDir(); err == nil && strings.HasPrefix(exe, home+string(os.PathSeparator)) {
		exe = "~" + exe[len(home):]
	}
	return exe
}

var toonNumeric = regexp.MustCompile(`^[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$`)

// toonString is a string as TOON writes a field's value: bare unless a reader would take it for
// something else.
func toonString(s string) string {
	needs := s == "" || strings.ContainsAny(s[:1], " \t-#") || strings.ContainsAny(s[len(s)-1:], " \t") ||
		s == "true" || s == "false" || s == "null" || strings.ContainsAny(s, `,:"\[]{}`) || toonNumeric.MatchString(s)
	for _, r := range s {
		if r < 0x20 {
			needs = true
		}
	}
	if !needs {
		return s
	}
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '\\':
			b.WriteString(`\\`)
		case '"':
			b.WriteString(`\"`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		default:
			if r < 0x20 {
				fmt.Fprintf(&b, `\u%04x`, r)
			} else {
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
	return b.String()
}

// pyDumps is Python's json.dumps(value, ensure_ascii=False): ", " and ": " between the parts.
func pyDumps(v any) string {
	switch t := v.(type) {
	case ordered:
		parts := make([]string, len(t))
		for i, p := range t {
			parts[i] = pyDumps(p.key) + ": " + pyDumps(p.value)
		}
		return "{" + strings.Join(parts, ", ") + "}"
	case []string:
		parts := make([]string, len(t))
		for i, s := range t {
			parts[i] = pyDumps(s)
		}
		return "[" + strings.Join(parts, ", ") + "]"
	}
	data, _ := marshalCompact(v)
	return string(data)
}
