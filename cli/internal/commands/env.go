// Package commands holds one file per command group. Every command is HTTP calls to the registry
// and printing; the registry owns the logic.
package commands

import (
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/config"
	"github.com/superdesigndev/treg/cli/internal/output"
	"github.com/superdesigndev/treg/cli/internal/term"
)

// Env is one invocation: its config, the global flags and the facts it records for telemetry.
type Env struct {
	Cfg  *config.Config
	JSON bool
	// OrgOverride is the global --org, for this invocation only.
	OrgOverride string
	Out         *output.Printer
	Telemetry   map[string]string
}

func NewEnv(cfg *config.Config, json bool, org string) *Env {
	return &Env{Cfg: cfg, JSON: json, OrgOverride: org, Out: output.NewPrinter(json, org), Telemetry: map[string]string{}}
}

// EffectiveOrg is --org, else TREG_ORG, else the stored active team.
func (e *Env) EffectiveOrg() string {
	if e.OrgOverride != "" {
		return e.OrgOverride
	}
	if v := os.Getenv("TREG_ORG"); v != "" {
		return v
	}
	return e.Cfg.ActiveOrg
}

func (e *Env) Registry() string { return api.RegistryURL(e.Cfg) }

// Client talks to the registry as this CLI's identity: TREG_TOKEN, else the stored token.
func (e *Env) Client() *api.Client {
	return api.New(e.Registry(), api.Token(e.Cfg), e.EffectiveOrg(), e.JSON)
}

// AnonymousClient sends no token: the login routes and the public catalog.
func (e *Env) AnonymousClient() *api.Client {
	return api.New(e.Registry(), "", "", e.JSON)
}

func (e *Env) Colors() term.Palette { return term.Colors() }

func (e *Env) Save() error {
	if err := e.Cfg.Save(); err != nil {
		return output.Fail("treg: could not save %s: %v", config.Path(), err)
	}
	return nil
}

// Note records a fixed-vocabulary fact for this invocation's telemetry.
func (e *Env) Note(key, value string) { e.Telemetry[key] = value }

// unreachable turns a transport failure into the one-line refusal the CLI prints.
func (e *Env) unreachable(err error) error {
	return output.Fail("treg: could not reach %s: %s", e.Registry(), transportReason(err))
}

// transportReason is why a request got no answer, in the system's words, without the operation
// and addresses Go's error also names.
func transportReason(err error) string {
	if dns, ok := errors.AsType[*net.DNSError](err); ok {
		return dns.Err
	}
	var nerr net.Error
	if errors.As(err, &nerr) && nerr.Timeout() {
		return "timed out"
	}
	if sys, ok := errors.AsType[*os.SyscallError](err); ok {
		var op *net.OpError
		if errors.As(err, &op) && op.Op == "proxyconnect" {
			return "proxy " + sys.Err.Error()
		}
		return sys.Err.Error()
	}
	if u, ok := errors.AsType[*url.Error](err); ok {
		return u.Err.Error()
	}
	return err.Error()
}

func (e *Env) do(c *api.Client, r api.Request) (*api.Response, error) {
	resp, err := c.Do(r)
	if err != nil {
		return nil, e.unreachable(err)
	}
	return resp, nil
}

func outf(format string, a ...any) { fmt.Fprintf(os.Stdout, format, a...) }

func outln(a ...any) { fmt.Fprintln(os.Stdout, a...) }

func errln(a ...any) { fmt.Fprintln(os.Stderr, a...) }
