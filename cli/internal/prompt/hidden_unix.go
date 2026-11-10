//go:build darwin || linux

package prompt

import (
	"errors"
	"io"

	"golang.org/x/sys/unix"
)

// readHidden reads one line from a terminal with echo off, as getpass does: a line ended by
// Ctrl-D is kept, and Ctrl-D on an empty line is the end of input.
func readHidden(fd int) ([]byte, error) {
	old, err := unix.IoctlGetTermios(fd, getTermios)
	if err != nil {
		return nil, err
	}
	hidden := *old
	hidden.Lflag &^= unix.ECHO
	hidden.Lflag |= unix.ICANON | unix.ISIG
	hidden.Iflag |= unix.ICRNL
	if err := unix.IoctlSetTermios(fd, setTermios, &hidden); err != nil {
		return nil, err
	}
	defer unix.IoctlSetTermios(fd, setTermios, old)
	var line []byte
	buf := make([]byte, 1)
	for {
		n, err := unix.Read(fd, buf)
		if errors.Is(err, unix.EINTR) {
			continue
		}
		if err != nil {
			return nil, err
		}
		if n == 0 {
			if len(line) == 0 {
				return nil, io.EOF
			}
			return line, nil
		}
		if buf[0] == '\n' {
			return line, nil
		}
		line = append(line, buf[0])
	}
}
