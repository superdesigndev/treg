package commands

import (
	"mime"
	"path/filepath"
	"strings"
)

// mediaTypes covers the formats `treg host` serves to vendors on machines whose type tables lack
// them (Go's built-in table has only web formats; the rest comes from the system's).
var mediaTypes = map[string]string{
	".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
	".webp": "image/webp", ".avif": "image/avif", ".heic": "image/heic", ".svg": "image/svg+xml",
	".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".aac": "audio/aac",
	".ogg": "audio/ogg", ".flac": "audio/flac", ".opus": "audio/opus",
	".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".m4v": "video/x-m4v",
	".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
}

// guessMediaType is the media type a file name's extension names, or "".
func guessMediaType(name string) string {
	ext := strings.ToLower(filepath.Ext(name))
	if ext == "" || ext == name {
		return ""
	}
	if t, ok := mediaTypes[ext]; ok {
		return t
	}
	t, _, _ := strings.Cut(mime.TypeByExtension(ext), ";")
	return strings.TrimSpace(t)
}
