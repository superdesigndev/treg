// Package buildinfo names this build. Release builds set Version (and UpdateKey) with
// -ldflags "-X github.com/superdesigndev/treg/cli/internal/buildinfo.Version=<version>".
package buildinfo

import "runtime"

var Version = "dev"

// UpdateKey is the minisign public key release SHA256SUMS are signed with, set at release time
// with -X .../buildinfo.UpdateKey=<base64 key>. A build without it never replaces itself.
var UpdateKey = ""

func UserAgent() string {
	return "treg/" + Version + " (" + runtime.GOOS + "/" + runtime.GOARCH + ")"
}
