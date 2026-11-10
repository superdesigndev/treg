// Package agents knows where each coding agent on this machine keeps its skills and its MCP server
// list, and writes treg into them.
//
// Paths honour the variables the agents themselves read: CLAUDE_CONFIG_DIR, CODEX_HOME and
// XDG_CONFIG_HOME. Every agent below keeps the same home-relative layout on Windows, under
// %USERPROFILE%.
package agents

import (
	"os"
	"path/filepath"
	"strings"
)

func home() string {
	h, err := os.UserHomeDir()
	if err != nil {
		return "."
	}
	return h
}

func envDir(name, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		return v
	}
	return fallback
}

func configHome() string { return envDir("XDG_CONFIG_HOME", filepath.Join(home(), ".config")) }

func claudeHome() string { return envDir("CLAUDE_CONFIG_DIR", filepath.Join(home(), ".claude")) }

func codexHome() string { return envDir("CODEX_HOME", filepath.Join(home(), ".codex")) }

// StandardProjectDir is the shared project convention most agents read.
const StandardProjectDir = ".agents/skills"

// Agent is one coding agent's skill locations: a repo-relative project dir, a per-user global dir,
// and a marker path whose existence means the agent is installed for this user.
type Agent struct {
	Name, Display, Project string
	Global, Marker         func() string
}

func under(parts ...string) func() string {
	return func() string { return filepath.Join(append([]string{home()}, parts...)...) }
}

func inConfig(parts ...string) func() string {
	return func() string { return filepath.Join(append([]string{configHome()}, parts...)...) }
}

// Known is every agent treg installs skills for, in the order output lists them.
var Known = []Agent{
	{"claude-code", "Claude Code", ".claude/skills", func() string { return filepath.Join(claudeHome(), "skills") }, claudeHome},
	{"cursor", "Cursor", StandardProjectDir, under(".cursor", "skills"), under(".cursor")},
	{"codex", "Codex", StandardProjectDir, func() string { return filepath.Join(codexHome(), "skills") }, codexHome},
	{"gemini-cli", "Gemini CLI", StandardProjectDir, under(".gemini", "skills"), under(".gemini")},
	{"github-copilot", "GitHub Copilot", StandardProjectDir, under(".copilot", "skills"), under(".copilot")},
	{"opencode", "OpenCode", StandardProjectDir, inConfig("opencode", "skills"), inConfig("opencode")},
	{"amp", "Amp", StandardProjectDir, inConfig("agents", "skills"), inConfig("amp")},
	{"cline", "Cline", StandardProjectDir, under(".agents", "skills"), under(".cline")},
	{"warp", "Warp", StandardProjectDir, under(".agents", "skills"), under(".warp")},
	{"zed", "Zed", StandardProjectDir, under(".agents", "skills"), inConfig("zed")},
	{"windsurf", "Windsurf", ".windsurf/skills", under(".codeium", "windsurf", "skills"), under(".codeium", "windsurf")},
	{"roo", "Roo Code", ".roo/skills", under(".roo", "skills"), under(".roo")},
	{"continue", "Continue", ".continue/skills", under(".continue", "skills"), under(".continue")},
	{"qwen-code", "Qwen Code", ".qwen/skills", under(".qwen", "skills"), under(".qwen")},
	{"kilo", "Kilo Code", ".kilocode/skills", under(".kilocode", "skills"), under(".kilocode")},
	{"goose", "Goose", ".goose/skills", inConfig("goose", "skills"), inConfig("goose")},
}

// DefaultProjectDirs is the fan-out when no agent is named: the shared standard and Claude Code.
var DefaultProjectDirs = []string{StandardProjectDir, ".claude/skills"}

func Find(name string) (Agent, bool) {
	for _, a := range Known {
		if a.Name == name {
			return a, true
		}
	}
	return Agent{}, false
}

func Names() []string {
	names := make([]string, len(Known))
	for i, a := range Known {
		names[i] = a.Name
	}
	return names
}

func exists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

func Exists(path string) bool { return exists(path) }

// Resolve is Python's Path.resolve(): absolute, every existing symlink followed.
func Resolve(p string) string {
	abs, err := filepath.Abs(p)
	if err != nil {
		return p
	}
	rest := ""
	for cur := abs; ; {
		if real, err := filepath.EvalSymlinks(cur); err == nil {
			if rest == "" {
				return real
			}
			return filepath.Join(real, rest)
		}
		parent := filepath.Dir(cur)
		if parent == cur {
			return abs
		}
		rest = filepath.Join(filepath.Base(cur), rest)
		cur = parent
	}
}

// Detected is the agents whose marker exists here, in table order.
func Detected() []string {
	var out []string
	for _, a := range Known {
		if exists(a.Marker()) {
			out = append(out, a.Name)
		}
	}
	return out
}

func dedupe(paths []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, p := range paths {
		if !seen[p] {
			seen[p] = true
			out = append(out, p)
		}
	}
	return out
}

// Targets are the base directories skills are written into, deduped. An explicit dir wins; then one
// named agent; then every agent; then the global dirs of the detected agents; else the default
// project pair. ok is false for an unknown agent name.
func Targets(explicitDir, agent string, global, allAgents bool) (dirs []string, ok bool) {
	if explicitDir != "" {
		return []string{explicitDir}, true
	}
	if agent != "" {
		a, found := Find(agent)
		if !found {
			return nil, false
		}
		if global {
			return []string{a.Global()}, true
		}
		return []string{filepath.FromSlash(a.Project)}, true
	}
	if allAgents {
		for _, a := range Known {
			if global {
				dirs = append(dirs, a.Global())
			} else {
				dirs = append(dirs, filepath.FromSlash(a.Project))
			}
		}
		return dedupe(dirs), true
	}
	if global {
		for _, name := range Detected() {
			a, _ := Find(name)
			dirs = append(dirs, a.Global())
		}
		if len(dirs) > 0 {
			return dedupe(dirs), true
		}
		claude, _ := Find("claude-code")
		return dedupe([]string{claude.Global(), filepath.Join(home(), filepath.FromSlash(StandardProjectDir))}), true
	}
	for _, d := range DefaultProjectDirs {
		dirs = append(dirs, filepath.FromSlash(d))
	}
	return dedupe(dirs), true
}

// ReadText reads a file as Python's read_text does: universal newlines.
func ReadText(path string) (string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	return UniversalNewlines(string(raw)), nil
}

func UniversalNewlines(s string) string {
	if !strings.Contains(s, "\r") {
		return s
	}
	return strings.ReplaceAll(strings.ReplaceAll(s, "\r\n", "\n"), "\r", "\n")
}

// MkdirLeaf creates dir and its parents, giving only the last directory created the mode.
func MkdirLeaf(dir string, mode os.FileMode) error {
	if info, err := os.Stat(dir); err == nil && info.IsDir() {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(dir), 0o777); err != nil {
		return err
	}
	if err := os.Mkdir(dir, mode); err != nil && !os.IsExist(err) {
		return err
	}
	return nil
}
