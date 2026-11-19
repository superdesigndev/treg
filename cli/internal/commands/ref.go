package commands

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/superdesigndev/treg/cli/internal/config"
	"github.com/superdesigndev/treg/cli/internal/output"
)

// An affiliate partner's code (their Dub link key) for the team this person creates next, as the
// Python CLI keeps it: `treg login --ref`, `install.sh --ref` (which writes the file) or `TREG_REF`
// set it, and the registry reads it at team creation. Never a friend-referral code: the friend
// referral comes only from a `?ref=` link.
var refShape = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{2,31}$`)

func refPath() string { return config.Sibling("ref") }

func validRef(value string) string {
	value = strings.ToLower(strings.TrimSpace(value))
	if !refShape.MatchString(value) {
		return ""
	}
	return value
}

// pendingRef is the code a person chose with `treg login --ref` or `install.sh --ref`, then
// TREG_REF: an explicit choice saved on this machine outranks an environment default.
func pendingRef() string {
	if raw, err := os.ReadFile(refPath()); err == nil {
		if saved := validRef(string(raw)); saved != "" {
			return saved
		}
	}
	return validRef(os.Getenv("TREG_REF"))
}

func rememberRef(code string) error {
	if err := os.MkdirAll(filepath.Dir(refPath()), 0o700); err != nil {
		return err
	}
	return os.WriteFile(refPath(), []byte(code+"\n"), 0o644)
}

func forgetRef() { _ = os.Remove(refPath()) }

// refFlag is the `--ref` given on this command, validated; a malformed one fails rather than drop.
func refFlag(raw string, given bool) (string, error) {
	if !given {
		return "", nil
	}
	code := validRef(raw)
	if code == "" {
		return "", output.Fail("--ref '%s' is not an affiliate partner code (3-32 lowercase letters, digits or hyphens).", raw)
	}
	return code, nil
}

// settleRef runs after a sign-in: a person who already has a team made it without this code, so
// the code is spent. One without a team keeps it for `treg org create`.
func settleRef(cfg *config.Config) {
	if cfg.ActiveOrg != "" {
		forgetRef()
	}
}
