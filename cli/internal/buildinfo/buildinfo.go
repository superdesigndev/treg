// Package buildinfo names this build. Release builds set Version with
// -ldflags "-X github.com/superdesigndev/treg/cli/internal/buildinfo.Version=<version>".
package buildinfo

import "runtime"

var Version = "dev"

func UserAgent() string {
	return "treg/" + Version + " (" + runtime.GOOS + "/" + runtime.GOARCH + ")"
}
