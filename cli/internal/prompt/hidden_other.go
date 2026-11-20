//go:build !darwin && !linux

package prompt

import "golang.org/x/term"

func readHidden(fd int) ([]byte, error) {
	return term.ReadPassword(fd)
}
