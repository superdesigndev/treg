package commands

import "testing"

func TestGuessMediaType(t *testing.T) {
	for name, want := range map[string]string{
		"face.jpg": "image/jpeg", "FACE.JPEG": "image/jpeg", "a.png": "image/png", "clip.mp4": "video/mp4",
		"take.MOV": "video/quicktime", "voice.mp3": "audio/mpeg", "voice.wav": "audio/wav", "logo.svg": "image/svg+xml",
		"noext": "", ".png": "", "notes.qqq": "",
	} {
		if got := guessMediaType(name); got != want {
			t.Errorf("guessMediaType(%q) = %q, want %q", name, got, want)
		}
	}
}
