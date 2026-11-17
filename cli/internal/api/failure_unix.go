//go:build !windows

package api

import "syscall"

var (
	refused = []error{syscall.ECONNREFUSED}
	reset   = []error{syscall.ECONNRESET, syscall.ECONNABORTED}
)
