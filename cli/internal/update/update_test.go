package update

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"golang.org/x/crypto/blake2b"

	"github.com/superdesigndev/treg/cli/internal/buildinfo"
)

// signer makes minisign-format keys and signatures, as `minisign -S` does with its default
// (pre-hashed) algorithm.
type signer struct {
	id   [8]byte
	priv ed25519.PrivateKey
	pub  ed25519.PublicKey
}

func newSigner(t *testing.T) signer {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	s := signer{priv: priv, pub: pub}
	_, _ = rand.Read(s.id[:])
	return s
}

func (s signer) publicKey() string {
	return base64.StdEncoding.EncodeToString(append(append([]byte("Ed"), s.id[:]...), s.pub...))
}

func (s signer) sign(message []byte, comment string) string {
	h := blake2b.Sum512(message)
	sig := ed25519.Sign(s.priv, h[:])
	line := append(append([]byte("ED"), s.id[:]...), sig...)
	global := ed25519.Sign(s.priv, append(append([]byte{}, sig...), comment...))
	return "untrusted comment: signature\n" + base64.StdEncoding.EncodeToString(line) +
		"\ntrusted comment: " + comment + "\n" + base64.StdEncoding.EncodeToString(global) + "\n"
}

func tarball(t *testing.T, name string, data []byte) []byte {
	t.Helper()
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	_ = tw.WriteHeader(&tar.Header{Name: name, Mode: 0o755, Size: int64(len(data)), Typeflag: tar.TypeReg})
	_, _ = tw.Write(data)
	_ = tw.Close()
	_ = gz.Close()
	return buf.Bytes()
}

type release struct {
	srv     *httptest.Server
	files   map[string][]byte
	version string
}

func serveRelease(t *testing.T, s signer, version string, binary []byte) *release {
	t.Helper()
	archive := ArchiveName(version, runtime.GOOS, runtime.GOARCH)
	if runtime.GOOS == "windows" {
		t.Skip("the tarball fixture covers the Unix layout")
	}
	r := &release{files: map[string][]byte{}, version: version}
	r.files[archive] = tarball(t, "treg", binary)
	sum := sha256.Sum256(r.files[archive])
	r.files["SHA256SUMS"] = []byte(hex.EncodeToString(sum[:]) + "  " + archive + "\n")
	r.files["SHA256SUMS.minisig"] = []byte(s.sign(r.files["SHA256SUMS"], "treg "+version))
	r.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.URL.Path == "/cli/latest" {
			base := "http://" + req.Host + "/dl"
			fmt.Fprintf(w, `{"version":%q,"channel":%q,"archive":%q,"url":%q,"checksums_url":%q,"signature_url":%q}`,
				r.version, req.URL.Query().Get("channel"), archive, base+"/"+archive, base+"/SHA256SUMS", base+"/SHA256SUMS.minisig")
			return
		}
		data, ok := r.files[strings.TrimPrefix(req.URL.Path, "/dl/")]
		if !ok {
			http.NotFound(w, req)
			return
		}
		w.Write(data)
	}))
	t.Cleanup(r.srv.Close)
	return r
}

func withKey(t *testing.T, key string) {
	t.Helper()
	old := buildinfo.UpdateKey
	buildinfo.UpdateKey = key
	t.Cleanup(func() { buildinfo.UpdateKey = old })
}

func TestInstallsASignedRelease(t *testing.T) {
	s := newSigner(t)
	withKey(t, s.publicKey())
	r := serveRelease(t, s, "1.2.3", []byte("new binary"))
	exe := filepath.Join(t.TempDir(), "treg")
	os.WriteFile(exe, []byte("old binary"), 0o755)

	m, err := Latest(t.Context(), r.srv.URL, "beta")
	if err != nil || m.Version != "1.2.3" {
		t.Fatalf("Latest = %+v, %v", m, err)
	}
	if err := Install(t.Context(), m, exe); err != nil {
		t.Fatal(err)
	}
	got, _ := os.ReadFile(exe)
	if string(got) != "new binary" {
		t.Fatalf("exe holds %q", got)
	}
	if info, _ := os.Stat(exe); info.Mode().Perm()&0o100 == 0 {
		t.Fatal("the new binary is not executable")
	}
}

func TestRefusesWhatTheKeyDidNotSign(t *testing.T) {
	s := newSigner(t)
	other := newSigner(t)
	cases := map[string]func(r *release){
		"another key": func(r *release) { r.files["SHA256SUMS.minisig"] = []byte(other.sign(r.files["SHA256SUMS"], "x")) },
		"changed archive": func(r *release) {
			r.files[ArchiveName("1.2.3", runtime.GOOS, runtime.GOARCH)] = tarball(t, "treg", []byte("evil"))
		},
		"changed checksums": func(r *release) { r.files["SHA256SUMS"] = append(r.files["SHA256SUMS"], ' ') },
		"no signature":      func(r *release) { delete(r.files, "SHA256SUMS.minisig") },
	}
	for name, tamper := range cases {
		t.Run(name, func(t *testing.T) {
			withKey(t, s.publicKey())
			r := serveRelease(t, s, "1.2.3", []byte("new binary"))
			tamper(r)
			exe := filepath.Join(t.TempDir(), "treg")
			os.WriteFile(exe, []byte("old binary"), 0o755)
			m, _ := Latest(t.Context(), r.srv.URL, "stable")
			if err := Install(t.Context(), m, exe); err == nil {
				t.Fatal("installed a release the key did not sign")
			}
			if got, _ := os.ReadFile(exe); string(got) != "old binary" {
				t.Fatalf("exe changed to %q", got)
			}
		})
	}
}

func TestNoKeyNoUpdate(t *testing.T) {
	withKey(t, "")
	if err := Install(t.Context(), &Manifest{}, "/nonexistent"); err == nil || !strings.Contains(err.Error(), "release key") {
		t.Fatalf("err = %v", err)
	}
}

func TestNewer(t *testing.T) {
	for _, c := range []struct {
		a, b string
		want bool
	}{{"1.0.1", "1.0.0", true}, {"1.0.0", "1.0.0", false}, {"1.10.0", "1.9.9", true}, {"1.0", "1.0.0", false},
		{"0.9.0", "1.0.0", false}, {"1.0.0", "dev", true}, {"dev", "1.0.0", false},
		{"1.1.0-beta.1", "1.0.0", true}, {"1.1.0", "1.1.0-beta.2", true}, {"1.1.0-beta.2", "1.1.0", false},
		{"1.1.0-beta.10", "1.1.0-beta.9", true}, {"1.0.0", "1.1.0-beta.1", false}} {
		if got := Newer(c.a, c.b); got != c.want {
			t.Errorf("Newer(%s, %s) = %v", c.a, c.b, got)
		}
	}
}

func TestMethod(t *testing.T) {
	for exe, want := range map[string]string{
		"/opt/homebrew/Cellar/treg/1.0.0/bin/treg":                   "homebrew",
		"/home/linuxbrew/.linuxbrew/bin/treg":                        "homebrew",
		"/usr/lib/node_modules/@superdesign/treg-linux-x64/bin/treg": "npm",
		"/home/u/.local/bin/treg":                                    "self",
	} {
		if got := Method(exe); got != want {
			t.Errorf("Method(%s) = %s", exe, got)
		}
	}
}

func TestOldRegistryFallsBackOnlyWhenTheRouteIsMissing(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(404)
		w.Write([]byte(`{"detail":"no treg release on the beta channel"}`))
	}))
	defer srv.Close()
	if _, err := Latest(t.Context(), srv.URL, "beta"); err == nil || !strings.Contains(err.Error(), "no treg release") {
		t.Fatalf("err = %v", err)
	}
}

func TestOldRegistryUsesGitHubsBinaryReleases(t *testing.T) {
	registry := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(404)
		w.Write([]byte(`{"detail":"Not Found"}`))
	}))
	defer registry.Close()
	github := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`[{"tag_name":"v1.3.0","draft":true},{"tag_name":"v0.31.0"},` +
			`{"tag_name":"v1.2.0-beta.1","prerelease":true},{"tag_name":"v1.1.0"}]`))
	}))
	defer github.Close()
	defer func(old string) { gitHubAPI = old }(gitHubAPI)
	gitHubAPI = github.URL
	for channel, want := range map[string]string{"stable": "1.1.0", "beta": "1.2.0-beta.1"} {
		m, err := Latest(t.Context(), registry.URL, channel)
		if err != nil || m.Version != want || !strings.Contains(m.URL, "/releases/download/v"+want+"/") {
			t.Errorf("%s: %+v, %v", channel, m, err)
		}
	}
}
