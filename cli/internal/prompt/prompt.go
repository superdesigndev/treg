// Package prompt holds the terminal interactions: a spinner for slow steps, line and hidden input,
// a yes/no and a checkbox list. Each asks only on a terminal; without one the caller decides, so an agent's
// idle pipe is never read.
package prompt

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"runtime"
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

// Hidden is Python's getpass.getpass(): the prompt and the newline after the answer go to the
// controlling terminal, the typed text is not echoed, and Ctrl-C or Ctrl-D on an empty line is
// ErrEOF.
func Hidden(promptText string) (string, error) {
	in, out := os.Stdin, os.Stderr
	var opened *os.File
	if runtime.GOOS != "windows" {
		if f, err := os.OpenFile("/dev/tty", os.O_RDWR, 0); err == nil {
			opened, in, out = f, f, f
		}
	}
	fd := int(in.Fd())
	fmt.Fprint(out, promptText)
	state, err := xterm.GetState(fd)
	if err != nil {
		return "", ErrEOF
	}
	interrupted := make(chan os.Signal, 1)
	signal.Notify(interrupted, os.Interrupt)
	defer signal.Stop(interrupted)
	type result struct {
		secret []byte
		err    error
	}
	done := make(chan result, 1)
	go func() {
		secret, err := readHidden(fd)
		done <- result{secret, err}
	}()
	select {
	case r := <-done:
		if opened != nil {
			defer opened.Close()
		}
		if r.err != nil {
			return "", ErrEOF
		}
		fmt.Fprintln(out)
		return string(r.secret), nil
	case <-interrupted:
		// The read is still blocked on the terminal, and closing it would wait for that read, so
		// the descriptor is left to the process exit.
		_ = xterm.Restore(fd, state)
		return "", ErrEOF
	}
}

// Confirm is questionary.confirm with default no: y or n answers at once, enter takes the
// default, Ctrl-C is ErrEOF. It needs a terminal on stdin and stdout.
func Confirm(message string) (bool, error) {
	fd := int(os.Stdin.Fd())
	state, err := xterm.MakeRaw(fd)
	if err != nil {
		return false, ErrEOF
	}
	p := term.Colors()
	question := fmt.Sprintf("%s?%s %s%s%s", p.Accent, p.Reset, p.Bold, message, p.Reset)
	fmt.Fprintf(os.Stdout, "%s (y/N) ", question)
	answer, cancelled := false, false
	buf := make([]byte, 64)
read:
	for {
		n, err := os.Stdin.Read(buf)
		if err != nil || n == 0 {
			cancelled = true
			break
		}
		for _, key := range keys(buf[:n]) {
			switch key {
			case "\x03", "\x04":
				cancelled = true
				break read
			case "y", "Y":
				answer = true
				break read
			case "n", "N", "\r", "\n":
				break read
			}
		}
	}
	_ = xterm.Restore(fd, state)
	if cancelled {
		fmt.Fprintln(os.Stdout)
		return false, ErrEOF
	}
	word := "No"
	if answer {
		word = "Yes"
	}
	fmt.Fprintf(os.Stdout, "\r\x1b[2K%s %s%s%s\n", question, p.Accent, word, p.Reset)
	return answer, nil
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
	buf := make([]byte, 64)
	for {
		n, err := os.Stdin.Read(buf)
		if err != nil || n == 0 {
			return nil, false
		}
		for _, key := range keys(buf[:n]) {
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
		}
		draw(false)
	}
}

// keys splits what one read returned into key presses: a paste or fast typing delivers several
// at once, and an arrow key is an escape sequence of three bytes.
func keys(b []byte) []string {
	var out []string
	for i := 0; i < len(b); i++ {
		if b[i] == 0x1b && i+2 < len(b) && (b[i+1] == '[' || b[i+1] == 'O') {
			out = append(out, string(b[i:i+3]))
			i += 2
			continue
		}
		out = append(out, string(b[i]))
	}
	return out
}
