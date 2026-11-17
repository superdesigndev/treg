// Package update replaces this binary with the release its channel names: the registry's
// /cli/latest answer (GitHub's newest release when the registry has none), a download (from the
// release's mirror when the first source fails) whose SHA256SUMS carries a minisign signature from
// the key built into this binary, and an atomic swap.
package update

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json/v2"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"golang.org/x/mod/semver"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/buildinfo"
	"github.com/superdesigndev/treg/cli/internal/minisign"
)

const (
	Repo         = "superdesigndev/treg"
	maxDownload  = 200 << 20
	fetchTimeout = 5 * time.Minute
)

type Manifest struct {
	Version            string `json:"version"`
	Channel            string `json:"channel"`
	Archive            string `json:"archive"`
	URL                string `json:"url"`
	ChecksumsURL       string `json:"checksums_url"`
	SignatureURL       string `json:"signature_url"`
	MirrorURL          string `json:"mirror_url"`
	MirrorChecksumsURL string `json:"mirror_checksums_url"`
	MirrorSignatureURL string `json:"mirror_signature_url"`
}

// Download is where an update's files came from, in a usage event's words: the source that
// worked ("primary" or "mirror"), why the first failed and after how long, and why the mirror did.
type Download struct {
	Source       string
	Detail       string
	MirrorDetail string
	Seconds      int
}

// Failure is why an update did not install: the step that failed (no_key, mismatch, download,
// signature, checksum, archive, replace) and, for a download or a check, why in a usage event's
// words (a FailureKind, http_<status>, bad_signature, sums_missing, mismatch).
type Failure struct {
	Step   string
	Detail string
	Err    error
}

func (f *Failure) Error() string { return f.Err.Error() }
func (f *Failure) Unwrap() error { return f.Err }

var client = &http.Client{Timeout: fetchTimeout}

var gitHubAPI = "https://api.github.com"

// NoRelease is a channel with no release for this platform yet: during the beta, stable has none.
type NoRelease struct{ Channel string }

func (e *NoRelease) Error() string {
	return fmt.Sprintf("no treg release on the %s channel for %s/%s", e.Channel, runtime.GOOS, runtime.GOARCH)
}

// Latest asks the registry which release this channel runs on this platform, and GitHub when the
// registry is older than /cli/latest. Any other failure is a failed check: the channel the
// registry's operator holds is never left for GitHub's newest release.
func Latest(ctx context.Context, registry, channel string) (*Manifest, error) {
	q := url.Values{"channel": {channel}, "os": {runtime.GOOS}, "arch": {runtime.GOARCH}}
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(registry, "/")+"/cli/latest?"+q.Encode(), nil)
	req.Header.Set("User-Agent", buildinfo.UserAgent())
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("could not reach %s: %w", registry, err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	switch {
	case resp.StatusCode == http.StatusOK:
		var m Manifest
		if json.Unmarshal(body, &m) == nil && m.Version != "" && m.URL != "" {
			return &m, nil
		}
		return nil, fmt.Errorf("%s answered /cli/latest with an incomplete release", registry)
	case resp.StatusCode == http.StatusNotFound && bytes.Contains(body, []byte(`"Not Found"`)):
		return fromGitHub(ctx, channel)
	case resp.StatusCode == http.StatusNotFound:
		return nil, &NoRelease{Channel: channel}
	}
	return nil, &CheckStatus{Registry: registry, Code: resp.StatusCode}
}

// CheckStatus is a /cli/latest answer that names neither a release nor the lack of one.
type CheckStatus struct {
	Registry string
	Code     int
}

func (e *CheckStatus) Error() string {
	return fmt.Sprintf("%s answered /cli/latest with HTTP %d", e.Registry, e.Code)
}

func fromGitHub(ctx context.Context, channel string) (*Manifest, error) {
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, gitHubAPI+"/repos/"+Repo+"/releases?per_page=30", nil)
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("User-Agent", buildinfo.UserAgent())
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("could not reach GitHub releases: %w", err)
	}
	defer resp.Body.Close()
	var releases []struct {
		Tag        string `json:"tag_name"`
		Draft      bool   `json:"draft"`
		Prerelease bool   `json:"prerelease"`
	}
	if resp.StatusCode != http.StatusOK || json.UnmarshalRead(resp.Body, &releases) != nil {
		return nil, fmt.Errorf("GitHub releases answered %d", resp.StatusCode)
	}
	for _, r := range releases {
		version, ok := strings.CutPrefix(r.Tag, "v")
		if r.Draft || !ok || !IsBinaryRelease(version) || (channel == "stable" && r.Prerelease) {
			continue
		}
		base := "https://github.com/" + Repo + "/releases/download/" + r.Tag
		archive := ArchiveName(version, runtime.GOOS, runtime.GOARCH)
		return &Manifest{Version: version, Channel: channel, Archive: archive, URL: base + "/" + archive,
			ChecksumsURL: base + "/SHA256SUMS", SignatureURL: base + "/SHA256SUMS.minisig"}, nil
	}
	return nil, &NoRelease{Channel: channel}
}

func ArchiveName(version, goos, goarch string) string {
	ext := "tar.gz"
	if goos == "windows" {
		ext = "zip"
	}
	return fmt.Sprintf("treg_%s_%s_%s.%s", version, goos, goarch, ext)
}

func ExeName() string {
	if runtime.GOOS == "windows" {
		return "treg.exe"
	}
	return "treg"
}

type source struct{ name, archive, sums, signature string }

// Install downloads, verifies and swaps in the manifest's release over exe, taking the mirror's
// copy when the first source fails to download or verify. The error is a *Failure.
func Install(ctx context.Context, m *Manifest, exe string) (Download, error) {
	var d Download
	key := strings.TrimSpace(buildinfo.UpdateKey)
	if key == "" {
		return d, &Failure{Step: "no_key", Err: errors.New("this build carries no release key, so it cannot verify an update; reinstall with the install command")}
	}
	if want := ArchiveName(m.Version, runtime.GOOS, runtime.GOARCH); m.Archive != want {
		return d, &Failure{Step: "mismatch", Err: fmt.Errorf("the release answer names %s, not %s", m.Archive, want)}
	}
	pk, err := minisign.ParsePublicKey(key)
	if err != nil {
		return d, &Failure{Step: "no_key", Err: fmt.Errorf("this build's release key is malformed: %w", err)}
	}
	sources := []source{{"primary", m.URL, m.ChecksumsURL, m.SignatureURL}}
	if m.MirrorURL != "" && m.MirrorChecksumsURL != "" && m.MirrorSignatureURL != "" {
		sources = append(sources, source{"mirror", m.MirrorURL, m.MirrorChecksumsURL, m.MirrorSignatureURL})
	}
	started := time.Now()
	var archive []byte
	var first *Failure
	for _, s := range sources {
		got, err := verified(ctx, pk, m, s)
		if err == nil {
			archive, d.Source = got, s.name
			break
		}
		f, _ := errors.AsType[*Failure](err)
		if first == nil {
			first, d.Detail, d.Seconds = f, f.Detail, int(time.Since(started).Seconds())
		} else {
			d.MirrorDetail = f.Detail
			first = &Failure{Step: first.Step, Detail: first.Detail, Err: fmt.Errorf("%w; from the mirror: %v", first.Err, f)}
		}
	}
	if archive == nil {
		return d, first
	}
	binary, err := extract(m.Archive, archive)
	if err != nil {
		return d, &Failure{Step: "archive", Err: err}
	}
	if err := replace(exe, binary); err != nil {
		return d, &Failure{Step: "replace", Err: err}
	}
	return d, nil
}

// verified is one source's archive, once the release key signed its SHA256SUMS and the archive
// matches them.
func verified(ctx context.Context, pk minisign.PublicKey, m *Manifest, s source) ([]byte, error) {
	sums, err := fetch(ctx, s.sums)
	if err != nil {
		return nil, err
	}
	sig, err := fetch(ctx, s.signature)
	if err != nil {
		return nil, err
	}
	if err := minisign.Verify(pk, sums, string(sig)); err != nil {
		return nil, &Failure{Step: "signature", Detail: "bad_signature",
			Err: fmt.Errorf("SHA256SUMS of treg %s is not signed by treg's release key: %w", m.Version, err)}
	}
	want := checksumFor(sums, m.Archive)
	if want == "" {
		return nil, &Failure{Step: "checksum", Detail: "sums_missing", Err: fmt.Errorf("SHA256SUMS names no %s", m.Archive)}
	}
	archive, err := fetch(ctx, s.archive)
	if err != nil {
		return nil, err
	}
	got := sha256.Sum256(archive)
	if hex.EncodeToString(got[:]) != want {
		return nil, &Failure{Step: "checksum", Detail: "mismatch",
			Err: fmt.Errorf("%s does not match its SHA256SUMS entry", m.Archive)}
	}
	return archive, nil
}

func fetch(ctx context.Context, u string) ([]byte, error) {
	failed := func(err error) error {
		return &Failure{Step: "download", Detail: api.FailureKind(err), Err: fmt.Errorf("could not download %s: %w", u, err)}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, &Failure{Step: "download", Detail: "bad_url", Err: err}
	}
	req.Header.Set("User-Agent", buildinfo.UserAgent())
	resp, err := client.Do(req)
	if err != nil {
		return nil, failed(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, &Failure{Step: "download", Detail: fmt.Sprintf("http_%d", resp.StatusCode),
			Err: fmt.Errorf("could not download %s: HTTP %d", u, resp.StatusCode)}
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxDownload+1))
	if err != nil {
		return nil, failed(err)
	}
	if len(data) > maxDownload {
		return nil, &Failure{Step: "download", Detail: "too_large", Err: fmt.Errorf("%s is larger than any treg release", u)}
	}
	return data, nil
}

func checksumFor(sums []byte, name string) string {
	for line := range strings.SplitSeq(string(sums), "\n") {
		fields := strings.Fields(line)
		if len(fields) == 2 && strings.TrimPrefix(fields[1], "*") == name {
			return strings.ToLower(fields[0])
		}
	}
	return ""
}

func extract(name string, archive []byte) ([]byte, error) {
	want := ExeName()
	if strings.HasSuffix(name, ".zip") {
		zr, err := zip.NewReader(bytes.NewReader(archive), int64(len(archive)))
		if err != nil {
			return nil, err
		}
		for _, f := range zr.File {
			if filepath.Base(f.Name) == want && !f.FileInfo().IsDir() {
				rc, err := f.Open()
				if err != nil {
					return nil, err
				}
				defer rc.Close()
				return io.ReadAll(io.LimitReader(rc, maxDownload))
			}
		}
		return nil, fmt.Errorf("%s holds no %s", name, want)
	}
	gz, err := gzip.NewReader(bytes.NewReader(archive))
	if err != nil {
		return nil, err
	}
	tr := tar.NewReader(gz)
	for {
		h, err := tr.Next()
		if err == io.EOF {
			return nil, fmt.Errorf("%s holds no %s", name, want)
		}
		if err != nil {
			return nil, err
		}
		if h.Typeflag == tar.TypeReg && filepath.Base(h.Name) == want {
			return io.ReadAll(io.LimitReader(tr, maxDownload))
		}
	}
}

// replace writes the new binary beside exe and renames it over exe, so a crash never leaves a
// half-written treg and a running treg keeps its old file. Windows cannot replace a running
// executable, but can rename it; the next run removes the old copy.
func replace(exe string, binary []byte) error {
	dir := filepath.Dir(exe)
	tmp, err := os.CreateTemp(dir, ".treg-update-*")
	if err != nil {
		return fmt.Errorf("cannot write beside %s: %w", exe, err)
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(binary); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmp.Name(), 0o755); err != nil {
		return err
	}
	if runtime.GOOS == "windows" {
		old := exe + ".old"
		_ = os.Remove(old)
		if err := os.Rename(exe, old); err != nil {
			return err
		}
		if err := os.Rename(tmp.Name(), exe); err != nil {
			_ = os.Rename(old, exe)
			return err
		}
		return nil
	}
	return os.Rename(tmp.Name(), exe)
}

// RemoveLeftover deletes the copy a Windows update renamed aside.
func RemoveLeftover(exe string) {
	if runtime.GOOS == "windows" {
		_ = os.Remove(exe + ".old")
	}
}

// Newer reports whether release version a sorts after b as semantic versions do, a pre-release
// (1.1.0-beta.1) before its release; a version that is not one (a dev build) is older than any.
func Newer(a, b string) bool {
	va, vb := "v"+a, "v"+b
	if !semver.IsValid(va) || !semver.IsValid(vb) {
		return semver.IsValid(va) || (!semver.IsValid(vb) && a != b)
	}
	return semver.Compare(va, vb) > 0
}

// IsBinaryRelease is a version from 1.0.0 up: a 0.x version names a Python CLI or registry release.
func IsBinaryRelease(v string) bool {
	return semver.IsValid("v"+v) && semver.Major("v"+v) != "v0"
}
