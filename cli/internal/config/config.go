// Package config reads and writes ~/.treg/config.json in the format the Python CLI uses, so both
// can run against one file during the migration.
package config

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
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

	// Fields this version does not know, kept in their original order so a newer writer's
	// additions survive a save by this one.
	extra []field
	// A one-command `--org` exchange: Save writes these instead of the temporary token and team.
	once *saved
}

type field struct {
	key   string
	value json.RawMessage
}

type saved struct {
	token, activeOrg string
}

var known = map[string]bool{
	"base_url": true, "token": true, "email": true, "active_org": true, "identity": true, "admin_token": true,
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
	cfg := &Config{BaseURL: ProductionBaseURL}
	raw, err := os.ReadFile(Path())
	if err != nil {
		return cfg
	}
	fields, err := decodeObject(raw)
	if err != nil {
		return cfg
	}
	values := map[string]json.RawMessage{}
	for _, f := range fields {
		values[f.key] = f.value
		if !known[f.key] {
			cfg.extra = append(cfg.extra, f)
		}
	}
	if v, ok := values["base_url"]; ok {
		cfg.BaseURL = str(v)
	}
	cfg.Email = str(values["email"])
	cfg.ActiveOrg = str(values["active_org"])
	cfg.AdminToken = str(values["admin_token"])
	if orgs, ok := values["orgs"]; ok {
		// The legacy multi-org format: the active org's token becomes the one bearer.
		var byOrg map[string]struct {
			Token json.RawMessage `json:"token"`
		}
		_ = json.Unmarshal(orgs, &byOrg)
		cfg.Token = str(byOrg[cfg.ActiveOrg].Token)
		cfg.Identity = false
		cfg.extra = without(cfg.extra, "orgs")
		return cfg
	}
	cfg.Token = str(values["token"])
	var identity bool
	if json.Unmarshal(values["identity"], &identity) == nil {
		cfg.Identity = identity
	}
	return cfg
}

// UseOnce switches this invocation to another team's key without changing what Save writes.
func (c *Config) UseOnce(token, activeOrg string) {
	if c.once == nil {
		c.once = &saved{token: c.Token, activeOrg: c.ActiveOrg}
	}
	c.Token, c.ActiveOrg = token, activeOrg
}

// Save writes atomically (write, then rename) with mode 0600 in a 0700 directory.
func (c *Config) Save() error {
	path := Path()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	token, active := c.Token, c.ActiveOrg
	if c.once != nil {
		token, active = c.once.token, c.once.activeOrg
	}
	fields := []field{
		{"base_url", nullable(c.BaseURL)},
		{"token", nullable(token)},
		{"email", nullable(c.Email)},
		{"active_org", nullable(active)},
		{"identity", boolean(c.Identity)},
		{"admin_token", nullable(c.AdminToken)},
	}
	body, err := encodeObject(append(fields, c.extra...))
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, body, 0o600); err != nil {
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

func str(v json.RawMessage) string {
	var s string
	if json.Unmarshal(v, &s) != nil {
		return ""
	}
	return s
}

func nullable(s string) json.RawMessage {
	if s == "" {
		return json.RawMessage("null")
	}
	return quoted(s)
}

func quoted(s string) json.RawMessage {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(s)
	return bytes.TrimSuffix(buf.Bytes(), []byte("\n"))
}

func boolean(b bool) json.RawMessage {
	if b {
		return json.RawMessage("true")
	}
	return json.RawMessage("false")
}

func without(fields []field, key string) []field {
	out := fields[:0]
	for _, f := range fields {
		if f.key != key {
			out = append(out, f)
		}
	}
	return out
}

func decodeObject(raw []byte) ([]field, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	tok, err := dec.Token()
	if err != nil {
		return nil, err
	}
	if d, ok := tok.(json.Delim); !ok || d != '{' {
		return nil, errors.New("config is not a JSON object")
	}
	var fields []field
	for dec.More() {
		tok, err := dec.Token()
		if err != nil {
			return nil, err
		}
		key, _ := tok.(string)
		var value json.RawMessage
		if err := dec.Decode(&value); err != nil {
			return nil, err
		}
		fields = append(fields, field{key, value})
	}
	if _, err := dec.Token(); err != nil {
		return nil, err
	}
	if _, err := dec.Token(); err != io.EOF {
		return nil, errors.New("trailing data after the config object")
	}
	return fields, nil
}

// encodeObject writes the layout Python's json.dumps(indent=2) gives a flat object.
func encodeObject(fields []field) ([]byte, error) {
	var buf bytes.Buffer
	buf.WriteString("{")
	for i, f := range fields {
		if i > 0 {
			buf.WriteString(",")
		}
		key := quoted(f.key)
		var value bytes.Buffer
		if err := json.Indent(&value, f.value, "  ", "  "); err != nil {
			return nil, err
		}
		buf.WriteString("\n  ")
		buf.Write(key)
		buf.WriteString(": ")
		buf.Write(value.Bytes())
	}
	if len(fields) > 0 {
		buf.WriteString("\n")
	}
	buf.WriteString("}")
	return buf.Bytes(), nil
}
