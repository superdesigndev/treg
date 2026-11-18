package api

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"io"
	"net"
	"slices"
)

// FailureKind names why a request got no answer in a few fixed words, for usage events: never an
// address, a path or the system's message.
func FailureKind(err error) string {
	var nerr net.Error
	var op *net.OpError
	var unknownAuthority x509.UnknownAuthorityError
	var hostname x509.HostnameError
	var invalid x509.CertificateInvalidError
	var alert tls.AlertError
	is := func(targets []error) bool {
		return slices.ContainsFunc(targets, func(t error) bool { return errors.Is(err, t) })
	}
	switch {
	case errors.Is(err, context.DeadlineExceeded), errors.As(err, &nerr) && nerr.Timeout():
		return "timeout"
	case errors.Is(err, context.Canceled):
		return "canceled"
	case errors.As(err, new(*net.DNSError)):
		return "dns"
	case errors.As(err, &op) && op.Op == "proxyconnect":
		return "proxy"
	case is(refused):
		return "connection_refused"
	case is(reset):
		return "connection_reset"
	case errors.Is(err, io.ErrUnexpectedEOF), errors.Is(err, io.EOF):
		return "connection_closed"
	case errors.As(err, new(*tls.CertificateVerificationError)), errors.As(err, &unknownAuthority),
		errors.As(err, &hostname), errors.As(err, &invalid):
		return "certificate"
	case errors.As(err, new(tls.RecordHeaderError)), errors.As(err, &alert):
		return "tls"
	}
	return "other"
}
