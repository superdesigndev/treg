// Package pymime guesses a file's media type the way Python's mimetypes.guess_type does on the same
// machine: CPython's built-in table, overridden by every mime.types file it knows that exists.
package pymime

import (
	"bufio"
	"os"
	"runtime"
	"strings"
	"sync"
	"unicode"
)

var (
	once  sync.Once
	types map[string]string
)

func load() {
	types = make(map[string]string, len(defaultTypes))
	for k, v := range defaultTypes {
		types[k] = v
	}
	if runtime.GOOS == "windows" {
		return
	}
	for _, path := range knownFiles {
		if info, err := os.Stat(path); err != nil || !info.Mode().IsRegular() {
			continue
		}
		readTypes(path)
	}
}

// readTypes is MimeTypes.readfp: each line is a type and its extensions, `#` starts a comment, and
// a later file overrides an earlier one.
func readTypes(path string) {
	f, err := os.Open(path)
	if err != nil {
		return
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 64*1024), 1024*1024)
	for scanner.Scan() {
		words := strings.FieldsFunc(scanner.Text(), unicode.IsSpace)
		for i, w := range words {
			if strings.HasPrefix(w, "#") {
				words = words[:i]
				break
			}
		}
		if len(words) < 2 {
			continue
		}
		for _, suffix := range words[1:] {
			types["."+suffix] = words[0]
		}
	}
}

// GuessType is mimetypes.guess_type(name)[0] for a file name, or "" when it has no known type.
// A compression suffix (.gz, .bz2, ...) is skipped, as Python reports it as the encoding.
func GuessType(name string) string {
	once.Do(load)
	base, ext := splitext(name)
	for {
		mapped, ok := suffixMap[strings.ToLower(ext)]
		if !ok {
			break
		}
		base, ext = splitext(base + mapped)
	}
	if _, ok := encodingsMap[ext]; ok {
		base, ext = splitext(base)
	}
	return types[strings.ToLower(ext)]
}

// splitext is os.path.splitext: the extension starts at the last dot of the last path component,
// unless only dots precede it there.
func splitext(p string) (string, string) {
	sep := strings.LastIndex(p, "/")
	if runtime.GOOS == "windows" {
		sep = max(sep, strings.LastIndex(p, `\`))
	}
	dot := strings.LastIndex(p, ".")
	if dot <= sep {
		return p, ""
	}
	for i := sep + 1; i < dot; i++ {
		if p[i] != '.' {
			return p[:dot], p[dot:]
		}
	}
	return p, ""
}
