// Package prompt holds the terminal interactions: a spinner for slow steps, line and hidden input,
// and a checkbox list. Each asks only on a terminal; without one the caller decides, so an agent's
// idle pipe is never read.
package prompt

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	xterm "golang.org/x/term"

	"github.com/superdesigndev/treg/cli/internal/term"
)

// ErrEOF is a prompt answered by end of input or Ctrl-C.
var ErrEOF = errors.New("no input")

func StdinIsTerminal() bool { return term.IsTerminal(os.Stdin) }

var (
	spinMu     sync.Mutex
	spinActive bool
)

// Spin shows msg while a slow step runs and returns the call that ends it. On a terminal it
// animates; otherwise it prints one static line, so logs stay deterministic. A nested Spin is
// silent.
func Spin(msg string) (stop func()) {
	spinMu.Lock()
	if spinActive {
		spinMu.Unlock()
		return func() {}
	}
	spinActive = true
	spinMu.Unlock()
	release := func() {
		spinMu.Lock()
		spinActive = false
		spinMu.Unlock()
	}
	if !term.IsTerminal(os.Stdout) {
		fmt.Fprintf(os.Stdout, "  … %s\n", msg)
		return release
	}
	p := term.Colors()
	done := make(chan struct{})
	finished := make(chan struct{})
	go func() {
		defer close(finished)
		frames := []rune("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
		for i := 0; ; i++ {
			select {
			case <-done:
				return
			case <-time.After(80 * time.Millisecond):
			}
			fmt.Fprintf(os.Stdout, "\r  %s%c%s %s…", p.Accent, frames[i%len(frames)], p.Reset, msg)
		}
	}()
	return func() {
		close(done)
		select {
		case <-finished:
		case <-time.After(300 * time.Millisecond):
		}
		fmt.Fprint(os.Stdout, "\r"+strings.Repeat(" ", utf8.RuneCountInString(msg)+6)+"\r")
		release()
	}
}

var stdin = bufio.NewReader(os.Stdin)

// Line prints the prompt on stdout and reads one line, as Python's input() does.
func Line(promptText string) (string, error) {
	fmt.Fprint(os.Stdout, promptText)
	line, err := stdin.ReadString('\n')
	if err != nil && (line == "" || err != io.EOF) {
		return "", ErrEOF
	}
	return strings.TrimRight(line, "\r\n"), nil
}

// Hidden reads a secret from the terminal without echo, the prompt on stderr.
func Hidden(promptText string) (string, error) {
	fmt.Fprint(os.Stderr, promptText)
	raw, err := xterm.ReadPassword(int(os.Stdin.Fd()))
	fmt.Fprintln(os.Stderr)
	if err != nil {
		return "", ErrEOF
	}
	return string(raw), nil
}

// Choice is one checkbox row.
type Choice struct {
	Title   string
	Checked bool
}

// Checkbox lets the person tick rows with the arrow keys and space, confirm with enter. ok is
// false on Ctrl-C, Ctrl-D or Esc. It needs a terminal on stdin and stdout.
func Checkbox(message string, choices []Choice) (picked []int, ok bool) {
	fd := int(os.Stdin.Fd())
	state, err := xterm.MakeRaw(fd)
	if err != nil {
		return nil, false
	}
	defer xterm.Restore(fd, state)
	p := term.Colors()
	checked := make([]bool, len(choices))
	for i, c := range choices {
		checked[i] = c.Checked
	}
	cursor := 0
	out := os.Stdout
	draw := func(first bool) {
		if !first {
			fmt.Fprintf(out, "\x1b[%dA", len(choices)+1)
		}
		fmt.Fprintf(out, "\r\x1b[2K%s?%s %s%s%s %s(↑↓ move, space select, enter confirm)%s\r\n",
			p.Accent, p.Reset, p.Bold, message, p.Reset, p.Muted, p.Reset)
		for i, c := range choices {
			pointer, mark := " ", "○"
			if i == cursor {
				pointer = p.Accent + "❯" + p.Reset
			}
			if checked[i] {
				mark = "●"
			}
			fmt.Fprintf(out, "\r\x1b[2K %s %s %s\r\n", pointer, mark, c.Title)
		}
	}
	fmt.Fprint(out, "\x1b[?25l")
	defer fmt.Fprint(out, "\x1b[?25h")
	draw(true)
	buf := make([]byte, 8)
	for {
		n, err := os.Stdin.Read(buf)
		if err != nil || n == 0 {
			return nil, false
		}
		key := string(buf[:n])
		switch key {
		case "\x1b[A", "\x1bOA", "k":
			cursor = (cursor - 1 + len(choices)) % len(choices)
		case "\x1b[B", "\x1bOB", "j":
			cursor = (cursor + 1) % len(choices)
		case " ":
			checked[cursor] = !checked[cursor]
		case "a":
			all := true
			for _, c := range checked {
				all = all && c
			}
			for i := range checked {
				checked[i] = !all
			}
		case "i":
			for i := range checked {
				checked[i] = !checked[i]
			}
		case "\r", "\n":
			for i, c := range checked {
				if c {
					picked = append(picked, i)
				}
			}
			fmt.Fprintf(out, "\x1b[%dA\r\x1b[J%s?%s %s%s%s %s(%d selected)%s\r\n", len(choices)+1,
				p.Accent, p.Reset, p.Bold, message, p.Reset, p.Muted, len(picked), p.Reset)
			return picked, true
		case "\x03", "\x04", "\x1b":
			fmt.Fprintf(out, "\x1b[%dA\r\x1b[J%sCancelled by user%s\r\n", len(choices)+1, p.Muted, p.Reset)
			return nil, false
		}
		draw(false)
	}
}
