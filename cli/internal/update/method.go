package update

import (
	"encoding/json/v2"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/superdesigndev/treg/cli/internal/buildinfo"
	"github.com/superdesigndev/treg/cli/internal/config"
)

const checkEvery = 24 * time.Hour

// Method is who keeps this binary current: "homebrew" and "npm" upgrade it themselves; "self" is
// a binary the treg installer placed, which treg replaces.
func Method(exe string) string {
	p := filepath.ToSlash(exe)
	switch {
	case strings.Contains(p, "/Cellar/") || strings.HasPrefix(p, "/opt/homebrew/") ||
		strings.Contains(p, "/linuxbrew/") || strings.Contains(p, "/.linuxbrew/"):
		return "homebrew"
	case strings.Contains(p, "/node_modules/"):
		return "npm"
	}
	return "self"
}

// UpgradeHint is the command that upgrades a binary its package manager owns.
func UpgradeHint(method string) string {
	switch method {
	case "homebrew":
		return "brew upgrade treg"
	case "npm":
		return "npm install -g @superdesign/treg@latest"
	}
	return "treg update"
}

// Executable is this binary's real path, symlinks resolved.
func Executable() (string, error) {
	exe, err := os.Executable()
	if err != nil {
		return "", err
	}
	if real, err := filepath.EvalSymlinks(exe); err == nil {
		return real, nil
	}
	return exe, nil
}

type state struct {
	CheckedAt int64  `json:"checked_at"`
	Result    string `json:"result,omitempty"`
}

func statePath() string { return config.Sibling("update-state.json") }

func readState() state {
	var s state
	if raw, err := os.ReadFile(statePath()); err == nil {
		_ = json.Unmarshal(raw, &s)
	}
	return s
}

func writeState(s state) {
	data, _ := json.Marshal(s)
	_ = os.MkdirAll(filepath.Dir(statePath()), 0o700)
	_ = os.WriteFile(statePath(), data, 0o600)
}

// AutoEnabled: background updates run for a release build the treg installer placed, unless
// TREG_NO_AUTO_UPDATE or the config turns them off.
func AutoEnabled(cfg *config.Config, exe string) bool {
	if buildinfo.Version == "dev" || strings.TrimSpace(buildinfo.UpdateKey) == "" || cfg.NoAutoUpdate {
		return false
	}
	if v := strings.ToLower(os.Getenv("TREG_NO_AUTO_UPDATE")); v != "" && v != "0" && v != "false" {
		return false
	}
	return Method(exe) == "self"
}

// MaybeStartBackground starts a detached `treg __update` at most once a day, and on the first run
// as the `treg` on PATH on a machine that still has the Python CLI's package installed. The
// command that triggered it never waits for it.
func MaybeStartBackground(cfg *config.Config, exe string) {
	if !AutoEnabled(cfg, exe) {
		return
	}
	s := readState()
	due := time.Since(time.Unix(s.CheckedAt, 0)) >= checkEvery
	if !due && (legacyCleaned() || !isPathTreg(exe)) {
		return
	}
	s.CheckedAt = time.Now().Unix()
	writeState(s)
	cmd := exec.Command(exe, "__update")
	cmd.Stdin, cmd.Stdout, cmd.Stderr = nil, nil, nil
	detach(cmd)
	if cmd.Start() == nil {
		_ = cmd.Process.Release()
	}
}

// RecordResult keeps the last background update's outcome for `treg update` to show.
func RecordResult(result string) {
	s := readState()
	s.Result = time.Now().UTC().Format(time.RFC3339) + " " + result
	writeState(s)
}

func LastResult() string { return readState().Result }
