// Package term answers what the output streams are attached to and holds the CLI's colours.
package term

import (
	"os"

	"golang.org/x/term"
)

func IsTerminal(f *os.File) bool {
	return term.IsTerminal(int(f.Fd()))
}

// IsPipe reports a FIFO, the way an agent captures a command's output.
func IsPipe(f *os.File) bool {
	info, err := f.Stat()
	return err == nil && info.Mode()&os.ModeNamedPipe != 0
}

// Colours are ANSI only when stdout is a terminal and NO_COLOR is unset, as in the Python CLI.
type Palette struct {
	Accent, Green, Muted, Teal, Amber, Bold, Reset string
}

func Colors() Palette {
	if !IsTerminal(os.Stdout) || os.Getenv("NO_COLOR") != "" {
		return Palette{}
	}
	return Palette{
		Accent: "\033[38;2;224;112;63m",
		Green:  "\033[38;2;127;174;114m",
		Muted:  "\033[38;2;169;158;136m",
		Teal:   "\033[38;2;95;158;160m",
		Amber:  "\033[38;2;208;162;74m",
		Bold:   "\033[1m",
		Reset:  "\033[0m",
	}
}
