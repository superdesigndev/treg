package commands

import (
	"net"
	"testing"
	"time"
)

func TestTransportReasonIsTheSystemsReason(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	ln.Close()
	_, err = net.Dial("tcp", addr)
	if got := transportReason(err); got != "connection refused" {
		t.Errorf("refused: %q", got)
	}
	_, err = net.DialTimeout("tcp", "10.255.255.1:9", time.Nanosecond)
	if got := transportReason(err); got != "timed out" {
		t.Errorf("timeout: %q", got)
	}
	err = &net.OpError{Op: "dial", Err: &net.DNSError{Err: "no such host", Name: "x.invalid"}}
	if got := transportReason(err); got != "no such host" {
		t.Errorf("dns: %q", got)
	}
}
