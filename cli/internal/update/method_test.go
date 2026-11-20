//go:build !windows

package update

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/superdesigndev/treg/cli/internal/buildinfo"
	"github.com/superdesigndev/treg/cli/internal/config"
)

// starts counts the background updates a fake `treg` logged: it waits up to five seconds for want
// of them (a detached process can be slow to start), then a moment longer for any extra one.
func starts(t *testing.T, log string, want int) int {
	t.Helper()
	count := func() int {
		data, _ := os.ReadFile(log)
		return bytes.Count(data, []byte("__update\n"))
	}
	for deadline := time.Now().Add(5 * time.Second); count() < want && time.Now().Before(deadline); {
		time.Sleep(20 * time.Millisecond)
	}
	time.Sleep(300 * time.Millisecond)
	return count()
}

func TestFirstRunsStartAnUpdateAMinuteAtMost(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("TREG_CONFIG", filepath.Join(dir, "config.json"))
	t.Setenv("TREG_NO_AUTO_UPDATE", "")
	withKey(t, "a key")
	defer func(v string) { buildinfo.Version = v }(buildinfo.Version)
	buildinfo.Version = "1.0.0"
	log := filepath.Join(dir, "starts.log")
	exe := filepath.Join(dir, "treg")
	os.WriteFile(exe, []byte("#!/bin/sh\necho \"$*\" >> '"+log+"'\n"), 0o755)
	t.Setenv("PATH", dir+":/bin:/usr/bin")
	cfg := &config.Config{}
	minutesAgo := func(m time.Duration) {
		s := readState()
		s.CheckedAt = time.Now().Add(-m * time.Minute).Unix()
		writeState(s)
	}

	for range 5 {
		MaybeStartBackground(cfg, exe)
	}
	if n := starts(t, log, 1); n != 1 {
		t.Fatalf("a burst of first runs started %d updates", n)
	}
	minutesAgo(2)
	MaybeStartBackground(cfg, exe)
	if n := starts(t, log, 2); n != 2 {
		t.Fatalf("a minute later the Python CLI's removal was not retried (%d starts)", n)
	}
	os.WriteFile(config.Sibling(legacyMarker), nil, 0o600)
	minutesAgo(2)
	MaybeStartBackground(cfg, exe)
	if n := starts(t, log, 2); n != 2 {
		t.Fatalf("once removal has run, an update started before the day was out (%d starts)", n)
	}
	minutesAgo(25 * 60)
	MaybeStartBackground(cfg, exe)
	if n := starts(t, log, 3); n != 3 {
		t.Fatalf("the daily update did not start (%d starts)", n)
	}
}
