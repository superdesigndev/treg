package api

import "syscall"

// Winsock's error numbers: a refused or reset connection on Windows is not syscall.ECONNREFUSED.
var (
	refused = []error{syscall.Errno(10061)}                       // WSAECONNREFUSED
	reset   = []error{syscall.Errno(10054), syscall.Errno(10053)} // WSAECONNRESET, WSAECONNABORTED
)
