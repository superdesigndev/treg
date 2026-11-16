package update

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// twoSources serves one signed release under /primary/ and /mirror/; break_ makes one source's
// archive fail the way a network or a filter does.
type twoSources struct {
	srv    *httptest.Server
	files  map[string][]byte
	broken map[string]string
}

func serveTwoSources(t *testing.T, s signer, version string, binary []byte) (*twoSources, *Manifest) {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("the tarball fixture covers the Unix layout")
	}
	archive := ArchiveName(version, runtime.GOOS, runtime.GOARCH)
	r := &twoSources{files: map[string][]byte{}, broken: map[string]string{}}
	r.files[archive] = tarball(t, "treg", binary)
	sum := sha256.Sum256(r.files[archive])
	r.files["SHA256SUMS"] = []byte(hex.EncodeToString(sum[:]) + "  " + archive + "\n")
	r.files["SHA256SUMS.minisig"] = []byte(s.sign(r.files["SHA256SUMS"], "treg "+version))
	r.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		source, name, _ := strings.Cut(strings.TrimPrefix(req.URL.Path, "/"), "/")
		switch how := r.broken[source]; {
		case how == "missing":
			http.NotFound(w, req)
			return
		case how == "html" && name == archive:
			w.Write([]byte("<html>blocked</html>"))
			return
		}
		w.Write(r.files[name])
	}))
	t.Cleanup(r.srv.Close)
	at := func(source string) (string, string, string) {
		base := r.srv.URL + "/" + source
		return base + "/" + archive, base + "/SHA256SUMS", base + "/SHA256SUMS.minisig"
	}
	m := &Manifest{Version: version, Archive: archive}
	m.URL, m.ChecksumsURL, m.SignatureURL = at("primary")
	m.MirrorURL, m.MirrorChecksumsURL, m.MirrorSignatureURL = at("mirror")
	return r, m
}

// closedURL is an address nothing listens on.
func closedURL(t *testing.T) string {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := l.Addr().String()
	l.Close()
	return "http://" + addr
}

func TestAFailedFirstSourceFallsBackToTheMirror(t *testing.T) {
	for how, want := range map[string]string{"missing": "http_404", "html": "mismatch", "refused": "connection_refused"} {
		t.Run(how, func(t *testing.T) {
			s := newSigner(t)
			withKey(t, s.publicKey())
			r, m := serveTwoSources(t, s, "1.2.3", []byte("new binary"))
			if how == "refused" {
				m.URL = closedURL(t) + "/" + m.Archive
			} else {
				r.broken["primary"] = how
			}
			exe := filepath.Join(t.TempDir(), "treg")
			os.WriteFile(exe, []byte("old binary"), 0o755)

			d, err := Install(t.Context(), m, exe)
			if err != nil {
				t.Fatal(err)
			}
			if d.Source != "mirror" || d.Detail != want || d.MirrorDetail != "" {
				t.Fatalf("download = %+v, want the mirror after %s", d, want)
			}
			if got, _ := os.ReadFile(exe); string(got) != "new binary" {
				t.Fatalf("exe holds %q", got)
			}
		})
	}
}

func TestAWorkingFirstSourceIsTheOnlyOneAsked(t *testing.T) {
	s := newSigner(t)
	withKey(t, s.publicKey())
	r, m := serveTwoSources(t, s, "1.2.3", []byte("new binary"))
	r.broken["mirror"] = "missing"
	exe := filepath.Join(t.TempDir(), "treg")
	os.WriteFile(exe, []byte("old binary"), 0o755)
	if d, err := Install(t.Context(), m, exe); err != nil || d != (Download{Source: "primary"}) {
		t.Fatalf("download = %+v, %v", d, err)
	}
}

func TestBothSourcesFailingLeavesTheBinaryAndSaysWhy(t *testing.T) {
	s := newSigner(t)
	withKey(t, s.publicKey())
	r, m := serveTwoSources(t, s, "1.2.3", []byte("new binary"))
	r.broken["primary"], r.broken["mirror"] = "missing", "html"
	exe := filepath.Join(t.TempDir(), "treg")
	os.WriteFile(exe, []byte("old binary"), 0o755)

	d, err := Install(t.Context(), m, exe)
	f, ok := errors.AsType[*Failure](err)
	if !ok || f.Step != "download" || f.Detail != "http_404" || !strings.Contains(err.Error(), "from the mirror") {
		t.Fatalf("err = %#v", err)
	}
	if d.Source != "" || d.Detail != "http_404" || d.MirrorDetail != "mismatch" {
		t.Fatalf("download = %+v", d)
	}
	if got, _ := os.ReadFile(exe); string(got) != "old binary" {
		t.Fatalf("exe changed to %q", got)
	}
}
