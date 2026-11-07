// Package config reads and writes ~/.treg/config.json in the format the Python CLI uses, so both
// can run against one file during the migration.
package config

import (
	"os"
	"path/filepath"
	"strings"

	"github.com/superdesigndev/treg/cli/internal/pyjson"
)

const ProductionBaseURL = "https://treg.to"

// Config is one machine's CLI state. Empty strings are written as JSON null, as Python writes None.
type Config struct {
	BaseURL    string
	Token      string
	Email      string
	ActiveOrg  string
	Identity   bool
	AdminToken string
	// Channel is the release channel this binary follows: "stable" (empty) or "beta".
	Channel string
	// NoAutoUpdate turns off background updates for this machine.
	NoAutoUpdate bool

	// file is the config as read, so a save keeps the fields this version does not know, where
	// they were, and a newer writer's additions survive.
	file *pyjson.Object
	// A one-command `--org` exchange: Save writes these instead of the temporary token and team.
	once *saved
}

type saved struct {
	token, activeOrg string
}

// Path is TREG_CONFIG when set, else ~/.treg/config.json.
func Path() string {
	if p := os.Getenv("TREG_CONFIG"); p != "" {
		return expandUser(p)
	}
	home, err := os.UserHomeDir()
	if err != nil {
		home = "."
	}
	return filepath.Join(home, ".treg", "config.json")
}

// Sibling is a file beside the config, where the CLI keeps its other state.
func Sibling(name string) string {
	return filepath.Join(filepath.Dir(Path()), name)
}

func expandUser(p string) string {
	if p != "~" && !strings.HasPrefix(p, "~/") && !strings.HasPrefix(p, "~"+string(filepath.Separator)) {
		return p
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return p
	}
	return filepath.Join(home, p[1:])
}

// Load never fails: a missing, corrupt or half-written file reads as a fresh config, so it cannot
// brick every command, login and logout included.
func Load() *Config {
	cfg := &Config{BaseURL: ProductionBaseURL, file: pyjson.NewObject()}
	raw, err := os.ReadFile(Path())
	if err != nil {
		return cfg
	}
	v, err := pyjson.Decode(raw)
	file, ok := v.(*pyjson.Object)
	if err != nil || !ok {
		return cfg
	}
	cfg.file = file
	if _, ok := file.Get("base_url"); ok {
		cfg.BaseURL = file.String("base_url")
	}
	cfg.Email = file.String("email")
	cfg.ActiveOrg = file.String("active_org")
	cfg.AdminToken = file.String("admin_token")
	cfg.Channel = file.String("channel")
	cfg.NoAutoUpdate = file.Value("auto_update") == false
	if orgs, ok := file.Value("orgs").(*pyjson.Object); ok {
		// The legacy multi-org format: the active org's token becomes the one bearer.
		active, _ := orgs.Value(cfg.ActiveOrg).(*pyjson.Object)
		cfg.Token = active.String("token")
		file.Delete("orgs")
		return cfg
	}
	cfg.Token = file.String("token")
	cfg.Identity = file.Value("identity") == true
	return cfg
}

// UseOnce switches this invocation to another team's key without changing what Save writes.
func (c *Config) UseOnce(token, activeOrg string) {
	if c.once == nil {
		c.once = &saved{token: c.Token, activeOrg: c.ActiveOrg}
	}
	c.Token, c.ActiveOrg = token, activeOrg
}

// Save writes atomically (write, then rename) with mode 0600 in a 0700 directory, in the layout
// of Python's json.dumps(indent=2).
func (c *Config) Save() error {
	path := Path()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	token, active := c.Token, c.ActiveOrg
	if c.once != nil {
		token, active = c.once.token, c.once.activeOrg
	}
	file := c.file.Copy()
	file.Set("base_url", nullable(c.BaseURL))
	file.Set("token", nullable(token))
	file.Set("email", nullable(c.Email))
	file.Set("active_org", nullable(active))
	file.Set("identity", c.Identity)
	file.Set("admin_token", nullable(c.AdminToken))
	file.Delete("channel")
	if c.Channel != "" {
		file.Set("channel", c.Channel)
	}
	file.Delete("auto_update")
	if c.NoAutoUpdate {
		file.Set("auto_update", false)
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, []byte(pyjson.Dumps(file, pyjson.Pretty)), 0o600); err != nil {
		return err
	}
	if err := os.Chmod(tmp, 0o600); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		return err
	}
	return os.Chmod(path, 0o600)
}

func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}
