// Package minisign verifies minisign signatures (https://jedisct1.github.io/minisign/), the format
// treg releases sign their SHA256SUMS with.
package minisign

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"errors"
	"strings"

	"golang.org/x/crypto/blake2b"
)

type PublicKey struct {
	keyID [8]byte
	key   ed25519.PublicKey
}

// ParsePublicKey accepts a minisign public key file or its base64 line alone.
func ParsePublicKey(text string) (PublicKey, error) {
	var line string
	for l := range strings.SplitSeq(strings.TrimSpace(text), "\n") {
		l = strings.TrimSpace(l)
		if l != "" && !strings.HasPrefix(l, "untrusted comment:") {
			line = l
			break
		}
	}
	raw, err := base64.StdEncoding.DecodeString(line)
	if err != nil || len(raw) != 42 || string(raw[:2]) != "Ed" {
		return PublicKey{}, errors.New("not a minisign public key")
	}
	var pk PublicKey
	copy(pk.keyID[:], raw[2:10])
	pk.key = ed25519.PublicKey(raw[10:])
	return pk, nil
}

// Verify checks a .minisig file's signature over message, and its global signature over the
// trusted comment.
func Verify(pk PublicKey, message []byte, signature string) error {
	lines := strings.Split(strings.ReplaceAll(strings.TrimSpace(signature), "\r\n", "\n"), "\n")
	if len(lines) < 4 || !strings.HasPrefix(lines[0], "untrusted comment:") {
		return errors.New("not a minisign signature")
	}
	sig, err := base64.StdEncoding.DecodeString(strings.TrimSpace(lines[1]))
	if err != nil || len(sig) != 74 {
		return errors.New("not a minisign signature")
	}
	trusted, ok := strings.CutPrefix(lines[2], "trusted comment: ")
	if !ok {
		return errors.New("the signature has no trusted comment")
	}
	global, err := base64.StdEncoding.DecodeString(strings.TrimSpace(lines[3]))
	if err != nil || len(global) != ed25519.SignatureSize {
		return errors.New("the signature's global signature is malformed")
	}
	if !bytes.Equal(sig[2:10], pk.keyID[:]) {
		return errors.New("signed with a different key")
	}
	signed := message
	switch string(sig[:2]) {
	case "ED":
		sum := blake2b.Sum512(message)
		signed = sum[:]
	case "Ed":
	default:
		return errors.New("unknown minisign signature algorithm")
	}
	if !ed25519.Verify(pk.key, signed, sig[10:]) {
		return errors.New("the signature does not match")
	}
	if !ed25519.Verify(pk.key, append(append([]byte{}, sig[10:]...), trusted...), global) {
		return errors.New("the trusted comment's signature does not match")
	}
	return nil
}
