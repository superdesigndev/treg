package update

import (
	"bytes"
	"os"
	"os/exec"

	"github.com/superdesigndev/treg/cli/internal/config"
)

const legacyMarker = "python-cli-removed"

func legacyCleaned() bool {
	_, err := os.Stat(config.Sibling(legacyMarker))
	return err == nil
}

// RemovePythonCLI uninstalls the Python CLI's package (`tools-registry`) from uv and pipx once this
// binary has replaced it. Their uninstallers delete the `treg` entry point they created, which is
// now this binary's path, so a copy is kept and put back the moment it is gone.
func RemovePythonCLI(exe string) {
	if legacyCleaned() {
		return
	}
	defer func() { _ = os.WriteFile(config.Sibling(legacyMarker), nil, 0o600) }()
	type tool struct{ list, uninstall []string }
	var found []tool
	for _, t := range []tool{
		{[]string{"uv", "tool", "list"}, []string{"uv", "tool", "uninstall", "tools-registry"}},
		{[]string{"pipx", "list", "--short"}, []string{"pipx", "uninstall", "tools-registry"}},
	} {
		if _, err := exec.LookPath(t.list[0]); err != nil {
			continue
		}
		out, err := exec.Command(t.list[0], t.list[1:]...).Output()
		if err == nil && bytes.Contains(out, []byte("tools-registry")) {
			found = append(found, t)
		}
	}
	if len(found) == 0 {
		return
	}
	data, err := os.ReadFile(exe)
	if err != nil {
		return
	}
	aside := exe + ".keep"
	if os.WriteFile(aside, data, 0o755) != nil {
		return
	}
	defer os.Remove(aside)
	for _, t := range found {
		_ = exec.Command(t.uninstall[0], t.uninstall[1:]...).Run()
		if _, err := os.Stat(exe); err != nil {
			_ = os.Rename(aside, exe)
			_ = os.WriteFile(aside, data, 0o755)
		}
	}
}
