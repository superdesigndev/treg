// Package session names the agent task a CLI invocation belongs to: a hashed session id and an
// agent label, sent as X-Treg-Session and X-Treg-Agent. It matches the Python CLI's cli_session so
// one task keeps one id across both during the migration.
package session

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
)

const (
	maxHops    = 8
	idleWindow = 30 * time.Minute
)

type Session struct {
	ID    string
	Agent string
}

// Proc is one ancestor process: its pid, lower-cased base name and start time in seconds.
type Proc struct {
	PID     int
	Name    string
	Started int64
}

// Variables an agent sets for the processes it runs. Children inherit them, so one is read only
// once the process tree has shown that agent to be the nearest.
var sessionVars = map[string][]string{
	"claude-code": {"CLAUDE_CODE_SESSION_ID"},
	"codex":       {"CODEX_THREAD_ID", "CODEX_SESSION_ID"},
}

var presenceVars = []struct {
	agent string
	keys  []string
}{
	{"claude-code", []string{"CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CLAUDE_PID"}},
	{"codex", []string{"CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_CI"}},
	{"gemini-cli", []string{"GEMINI_CLI"}},
}

var safeID = regexp.MustCompile(`^[A-Za-z0-9._:-]{1,64}$`)

var (
	once    sync.Once
	current Session
)

// Current resolves this invocation's session once. It never fails.
func Current(configPath string) Session {
	once.Do(func() {
		current = Resolve(configPath, environ(), ancestors())
	})
	return current
}

func environ() map[string]string {
	env := map[string]string{}
	for _, kv := range os.Environ() {
		if k, v, ok := strings.Cut(kv, "="); ok {
			env[k] = v
		}
	}
	return env
}

func Resolve(configPath string, env map[string]string, procs []Proc) Session {
	agent, process := "", (*Proc)(nil)
	for i := range procs {
		if a := agentOf(procs[i], env); a != "" {
			agent, process = a, &procs[i]
			break
		}
	}
	if agent == "" && len(procs) == 0 {
		var present []string
		for _, p := range presenceVars {
			for _, k := range p.keys {
				if env[k] != "" {
					present = append(present, p.agent)
					break
				}
			}
		}
		if len(present) == 1 {
			agent = present[0]
		}
	}
	label := agent
	if label == "" {
		label = "unknown"
	}
	if given := strings.TrimSpace(env["TREG_SESSION_ID"]); given != "" {
		if safeID.MatchString(given) {
			return Session{given, label}
		}
		return Session{hash("env", given), label}
	}
	for _, k := range sessionVars[agent] {
		if raw := env[k]; raw != "" {
			return Session{hash(label, raw), label}
		}
	}
	if process != nil {
		return Session{hash(label, fmt.Sprintf("%d:%d", process.PID, process.Started)), label}
	}
	return Session{hash(label, idleWindowID(configPath)), label}
}

func agentOf(p Proc, env map[string]string) string {
	// A native Claude Code install runs as an executable named after its version, so its own
	// CLAUDE_PID is the reliable mark.
	switch {
	case p.Name == "claude" || env["CLAUDE_PID"] == fmt.Sprint(p.PID):
		return "claude-code"
	case strings.HasPrefix(p.Name, "codex"):
		return "codex"
	case p.Name == "opencode":
		return "opencode"
	case p.Name == "gemini" || (p.Name == "node" && env["GEMINI_CLI"] == "1"):
		return "gemini-cli"
	}
	return ""
}

func hash(agent, raw string) string {
	sum := sha256.Sum256([]byte(agent + ":" + raw))
	return hex.EncodeToString(sum[:])[:16]
}

func idleWindowID(configPath string) string {
	path := filepath.Join(filepath.Dir(configPath), "session-id")
	if info, err := os.Stat(path); err == nil && time.Since(info.ModTime()) < idleWindow {
		if raw, err := os.ReadFile(path); err == nil {
			if id, ok := canonicalUUID(strings.TrimSpace(string(raw))); ok {
				now := time.Now()
				_ = os.Chtimes(path, now, now)
				return id
			}
		}
	}
	id := NewUUID()
	_ = os.MkdirAll(filepath.Dir(path), 0o755)
	_ = os.WriteFile(path, []byte(id), 0o644)
	return id
}

// NewUUID is a random (version 4) UUID in its canonical lower-case form.
func NewUUID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	h := hex.EncodeToString(b[:])
	return h[0:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:]
}

var uuidShape = regexp.MustCompile(`^(?i)(urn:uuid:)?\{?([0-9a-f]{8})-?([0-9a-f]{4})-?([0-9a-f]{4})-?([0-9a-f]{4})-?([0-9a-f]{12})\}?$`)

// canonicalUUID accepts the spellings Python's uuid.UUID accepts and returns its str() form.
func canonicalUUID(s string) (string, bool) {
	m := uuidShape.FindStringSubmatch(s)
	if m == nil {
		return "", false
	}
	return strings.ToLower(m[2] + "-" + m[3] + "-" + m[4] + "-" + m[5] + "-" + m[6]), true
}

func ancestors() []Proc {
	var found []Proc
	pid := os.Getppid()
	for pid > 1 && len(found) < maxHops {
		p, parent, ok := readProc(pid)
		if !ok {
			break
		}
		found = append(found, p)
		pid = parent
	}
	return found
}

func normalName(raw string) string {
	return strings.ToLower(strings.TrimLeft(filepath.Base(strings.TrimSpace(raw)), "-"))
}
