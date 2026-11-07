//go:build !windows

package update

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/superdesigndev/treg/cli/internal/config"
)

// fakeTool is a `uv` or `pipx` that lists the Python CLI's package and, like the real ones,
// deletes the `treg` entry point it once created when the package is uninstalled.
func fakeTool(t *testing.T, dir, name, listing, exe, log string) {
	t.Helper()
	script := "#!/bin/sh\n" +
		"case \"$*\" in\n" +
		"  *uninstall*) echo \"" + name + " $*\" >> '" + log + "'; rm -f '" + exe + "' ;;\n" +
		"  *) printf '%s\\n' '" + listing + "' ;;\n" +
		"esac\n"
	if err := os.WriteFile(filepath.Join(dir, name), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
}

func TestRemovingThePythonCLIKeepsThisBinary(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("TREG_CONFIG", filepath.Join(dir, "config.json"))
	bin := filepath.Join(dir, "bin")
	os.Mkdir(bin, 0o755)
	exe := filepath.Join(dir, "treg")
	os.WriteFile(exe, []byte("the binary"), 0o755)
	log := filepath.Join(dir, "calls.log")
	fakeTool(t, bin, "uv", "tools-registry v0.29.0", exe, log)
	fakeTool(t, bin, "pipx", "tools-registry 0.29.0", exe, log)
	t.Setenv("PATH", dir+":"+bin+":/bin:/usr/bin")

	RemovePythonCLI(exe)

	if calls, _ := os.ReadFile(log); string(calls) != "uv tool uninstall tools-registry\npipx uninstall tools-registry\n" {
		t.Fatalf("uninstalls: %q", calls)
	}
	data, err := os.ReadFile(exe)
	if err != nil || string(data) != "the binary" {
		t.Fatalf("exe after the uninstalls: %q, %v", data, err)
	}
	if info, _ := os.Stat(exe); info.Mode().Perm()&0o100 == 0 {
		t.Fatalf("exe lost its execute bit: %v", info.Mode())
	}
	if _, err := os.Stat(exe + ".keep"); !os.IsNotExist(err) {
		t.Fatalf("the spare copy is left behind: %v", err)
	}

	os.Remove(log)
	RemovePythonCLI(exe)
	if _, err := os.Stat(log); !os.IsNotExist(err) {
		t.Fatal("a second run uninstalled again")
	}
}

func TestNothingToRemoveIsCheckedOnce(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("TREG_CONFIG", filepath.Join(dir, "config.json"))
	bin := filepath.Join(dir, "bin")
	os.Mkdir(bin, 0o755)
	exe := filepath.Join(dir, "treg")
	os.WriteFile(exe, []byte("the binary"), 0o755)
	log := filepath.Join(dir, "calls.log")
	fakeTool(t, bin, "uv", "ruff v0.9.0", exe, log)
	t.Setenv("PATH", dir+":"+bin+":/bin:/usr/bin")

	RemovePythonCLI(exe)

	if _, err := os.Stat(log); !os.IsNotExist(err) {
		t.Fatal("uninstalled a package that is not there")
	}
	if _, err := os.Stat(config.Sibling(legacyMarker)); err != nil {
		t.Fatalf("no marker: %v", err)
	}
}
