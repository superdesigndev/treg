package minisign

import (
	"os"
	"strings"
	"testing"
)

func load(t *testing.T, name string) string {
	t.Helper()
	data, err := os.ReadFile("testdata/" + name)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func TestVerifiesASignatureMadeByMinisign(t *testing.T) {
	pk, err := ParsePublicKey(load(t, "test.pub"))
	if err != nil {
		t.Fatal(err)
	}
	message := []byte(load(t, "SHA256SUMS"))
	sig := load(t, "SHA256SUMS.minisig")
	if err := Verify(pk, message, sig); err != nil {
		t.Fatalf("a genuine signature was refused: %v", err)
	}
	if err := Verify(pk, append(message, 'x'), sig); err == nil {
		t.Fatal("a changed message verified")
	}
	forged := strings.Replace(sig, "trusted comment: treg 1.0.0", "trusted comment: treg 9.9.9", 1)
	if err := Verify(pk, message, forged); err == nil {
		t.Fatal("a changed trusted comment verified")
	}
}

func TestRefusesAnotherKey(t *testing.T) {
	other, err := ParsePublicKey("RWQf6LRCGA9i53mlYecO4IzT51TGPpvWucNSCh1CBM0QTaLn73Y7GFO3")
	if err != nil {
		t.Fatal(err)
	}
	if err := Verify(other, []byte(load(t, "SHA256SUMS")), load(t, "SHA256SUMS.minisig")); err == nil {
		t.Fatal("a signature from another key verified")
	}
}

func TestRejectsMalformedInput(t *testing.T) {
	if _, err := ParsePublicKey("not a key"); err == nil {
		t.Fatal("garbage parsed as a key")
	}
	pk, _ := ParsePublicKey(load(t, "test.pub"))
	if err := Verify(pk, nil, "untrusted comment: x\n!!!\n"); err == nil {
		t.Fatal("garbage verified")
	}
}
