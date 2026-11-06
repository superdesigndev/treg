package pymime

import (
	"os"
	"path/filepath"
	"testing"
)

func withTypes(t *testing.T, table map[string]string) {
	t.Helper()
	once.Do(func() {})
	saved := types
	types = table
	t.Cleanup(func() { types = saved })
}

func TestGuessTypeFollowsPython(t *testing.T) {
	withTypes(t, defaultTypes)
	for name, want := range map[string]string{
		"face.jpg":    "image/jpeg",
		"FACE.JPG":    "image/jpeg",
		"a..png":      "image/png",
		"..png":       "",
		".png":        "",
		"archive.tgz": "application/x-tar",
		"a.tar.gz":    "application/x-tar",
		"logo.svgz":   "image/svg+xml",
		"notes.txt.Z": "text/plain",
		"notes.txt.z": "",
		"noext":       "",
		"clip.mp4":    "video/mp4",
		"voice.mp3":   "audio/mpeg",
	} {
		if got := GuessType(name); got != want {
			t.Errorf("GuessType(%q) = %q, want %q", name, got, want)
		}
	}
}

func TestMimeTypesFileOverrides(t *testing.T) {
	withTypes(t, map[string]string{".wav": "audio/x-wav"})
	path := filepath.Join(t.TempDir(), "mime.types")
	if err := os.WriteFile(path, []byte("# comment\naudio/wav wav WAVE # trailing\n\nbad\nimage/x-q q\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	readTypes(path)
	for name, want := range map[string]string{"a.wav": "audio/wav", "a.WAVE": "", "a.q": "image/x-q"} {
		if got := GuessType(name); got != want {
			t.Errorf("GuessType(%q) = %q, want %q", name, got, want)
		}
	}
}
