package skillscan

import (
	"encoding/json"
	"regexp"
	"strings"

	"github.com/superdesigndev/treg/cli/internal/pyjson"
)

var (
	binRE     = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._+-]*$`)
	envNameRE = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

	dangerousEnv = map[string]bool{
		"PATH": true, "PYTHONPATH": true, "PYTHONSTARTUP": true, "PYTHONHOME": true, "PYTHONEXECUTABLE": true, "NODE_OPTIONS": true,
		"BASH_ENV": true, "ENV": true, "IFS": true, "SHELL": true, "GIT_SSH": true, "GIT_SSH_COMMAND": true, "GIT_EXTERNAL_DIFF": true,
		"PERL5LIB": true, "PERL5OPT": true, "RUBYOPT": true, "RUBYLIB": true, "GEM_PATH": true, "CLASSPATH": true, "JAVA_TOOL_OPTIONS": true,
		"_JAVA_OPTIONS": true, "PS4": true, "PROMPT_COMMAND": true, "GLOBIGNORE": true, "CDPATH": true, "FPATH": true, "ZDOTDIR": true,
	}
	dangerousPrefixes = []string{"LD_", "DYLD_", "BASH_FUNC_"}
	authMechanisms    = []string{"env", "argv", "config_file", "device"}
	verdicts          = []string{"ok", "credential_invalid", "unknown_error"}
)

const (
	maxInject = 32
	maxList   = 64
)

// pyRegexValid: the registry compiles these patterns with Python's re. RE2 refuses lookaround and
// backreferences that Python accepts, so those count as valid.
func pyRegexValid(p string) bool {
	if _, err := regexp.Compile(p); err == nil {
		return true
	}
	for _, perl := range []string{"(?=", "(?!", "(?<=", "(?<!", "(?P=", "(?>", `\1`, `\2`, `\3`, `\4`, `\5`, `\6`, `\7`, `\8`, `\9`} {
		if strings.Contains(p, perl) {
			return true
		}
	}
	return false
}

func nonEmptyString(v any) bool {
	s, ok := v.(string)
	return ok && s != ""
}

func stringList(v any) ([]string, bool) {
	items, ok := v.([]any)
	if !ok {
		return nil, false
	}
	out := make([]string, 0, len(items))
	for _, item := range items {
		s, ok := item.(string)
		if !ok {
			return nil, false
		}
		out = append(out, s)
	}
	return out, true
}

func isInt(v any) bool {
	switch t := v.(type) {
	case bool:
		return true
	case json.Number:
		return !strings.ContainsAny(string(t), ".eE")
	}
	return false
}

// validCLIProfile is the registry's own check of a skill's local-run `cli` block, so a contract the
// registry would refuse is not offered for upload.
func validCLIProfile(v any) bool {
	cli, ok := v.(*pyjson.Object)
	if !ok {
		return false
	}
	for _, key := range []string{"enabled", "deny_defaults", "beta"} {
		if val, ok := cli.Get(key); ok {
			if _, isBool := val.(bool); !isBool {
				return false
			}
		}
	}
	if val, ok := cli.Get("auth_mechanism"); ok {
		s, _ := val.(string)
		if _, isStr := val.(string); !isStr || !contains(authMechanisms, s) {
			return false
		}
	}
	if det, ok := cli.Get("detect"); ok && det != nil {
		d, ok := det.(*pyjson.Object)
		if !ok {
			return false
		}
		if paths, ok := d.Get("config_paths"); ok && paths != nil {
			list, ok := stringList(paths)
			if !ok {
				return false
			}
			for _, p := range list {
				if p == "" {
					return false
				}
			}
		}
	}
	for _, key := range []string{"bin", "install", "package", "runtime"} {
		if val, ok := cli.Get(key); ok && !nonEmptyString(val) {
			return false
		}
	}
	if bin := cli.String("bin"); bin != "" && !binRE.MatchString(bin) {
		return false
	}
	for _, key := range []string{"deny", "noninteractive", "warnings"} {
		val, ok := cli.Get(key)
		if !ok || val == nil {
			continue
		}
		list, ok := stringList(val)
		if !ok || len(list) > maxList {
			return false
		}
	}
	if deny, ok := cli.Get("deny"); ok {
		list, _ := stringList(deny)
		for _, p := range list {
			if !pyRegexValid(p) {
				return false
			}
		}
	}
	if errs, ok := cli.Get("errors"); ok && errs != nil {
		items, ok := errs.([]any)
		if !ok {
			return false
		}
		for _, item := range items {
			e, ok := item.(*pyjson.Object)
			if !ok {
				return false
			}
			pat, isStr := func() (string, bool) { v, _ := e.Get("pattern"); s, ok := v.(string); return s, ok }()
			if !isStr || !pyRegexValid(pat) {
				return false
			}
			if verdict, ok := e.Get("verdict"); ok && verdict != nil {
				s, isStr := verdict.(string)
				if !isStr || !contains(verdicts, s) {
					return false
				}
			}
		}
	}
	inject, ok := cli.Get("inject")
	if !ok || inject == nil {
		return true
	}
	items, ok := inject.([]any)
	if !ok || len(items) > maxInject {
		return false
	}
	for _, item := range items {
		e, ok := item.(*pyjson.Object)
		if !ok {
			return false
		}
		via := "env"
		if v, ok := e.Get("via"); ok {
			s, isStr := v.(string)
			if !isStr {
				return false
			}
			via = s
		}
		switch via {
		case "env":
			name, isStr := func() (string, bool) { v, _ := e.Get("name"); s, ok := v.(string); return s, ok }()
			if !isStr || !envNameRE.MatchString(name) {
				return false
			}
			upper := strings.ToUpper(name)
			if dangerousEnv[upper] {
				return false
			}
			for _, p := range dangerousPrefixes {
				if strings.HasPrefix(upper, p) {
					return false
				}
			}
		case "argv":
			argv, _ := e.Get("argv")
			list, ok := stringList(argv)
			if !ok || len(list) == 0 {
				return false
			}
		default:
			return false
		}
		format := "{secret}"
		if v, ok := e.Get("format"); ok {
			s, isStr := v.(string)
			if !isStr {
				return false
			}
			format = s
		}
		if !strings.Contains(format, "{secret}") {
			return false
		}
		for _, key := range []string{"from_binding", "secret_field", "secret"} {
			if v, ok := e.Get(key); ok && !nonEmptyString(v) {
				return false
			}
		}
		if v, ok := e.Get("secret_id"); ok && !isInt(v) {
			return false
		}
	}
	return true
}
