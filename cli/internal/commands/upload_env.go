package commands

import (
	"cmp"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"os/signal"
	"strings"
	"time"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/prompt"
	"github.com/superdesigndev/treg/cli/internal/providers"
	"github.com/superdesigndev/treg/cli/internal/pyjson"
)

func (e *Env) uploadEnv(f uploadFlags, envPath string) error {
	catalog := e.uploadCatalog(f)
	text := readEnvText(envPath)
	dets := providers.ScanEnv(text, catalog)
	actions := providers.PlanActions(dets)
	var supported, deferred []*providers.Action
	for _, a := range actions {
		switch {
		case a.Supported:
			supported = append(supported, a)
		case a.Detection.Kind == "oauth_pair" && providers.OAuthReady(a.Detection):
		default:
			deferred = append(deferred, a)
		}
	}
	var oauthDets, unknowns []providers.Detection
	for _, d := range dets {
		if d.Kind == "oauth_pair" && providers.OAuthReady(d) {
			oauthDets = append(oauthDets, d)
		}
		if d.Kind == "unknown_secret" {
			unknowns = append(unknowns, d)
		}
	}
	outf("Scanned %s: %d key(s) to register, %d OAuth, %d other.\n\n", envPath, len(supported), len(oauthDets), len(deferred))
	var chosen []*providers.Action
	if len(supported) > 0 {
		picked, err := e.selectProviders(supported, f)
		if err != nil {
			return err
		}
		chosen = picked
	}
	if f.selectNames != "" && len(supported) > 0 && len(chosen) == 0 {
		outf("  (no detected provider key matched --select %s)\n", f.selectNames)
	}
	if f.dryRun {
		if len(chosen) > 0 {
			if f.scan {
				outln("Found - `treg upload` registers these:")
			} else {
				outln("DRY RUN - would register:")
			}
			for _, a := range chosen {
				outf("  ✓ %-14s %s   [%s: %s]  (secret %s)\n", a.ToolName, a.BaseURL,
					cmp.Or(a.Binding.String("name"), "-"), cmp.Or(a.Binding.String("format"), "-"), a.SecretName)
			}
		}
		if len(oauthDets) > 0 {
			if f.scan {
				outln("\nFound OAuth pairs - `treg upload` connects them one by one:")
			} else {
				outln("\nDRY RUN - would prompt to connect one by one:")
			}
			for _, d := range oauthDets {
				outf("  ◆ %-14s %s\n", d.Provider, strings.Join(d.Vars, " + "))
			}
		}
		if f.llm && len(unknowns) > 0 {
			var names []string
			for _, d := range unknowns {
				names = append(names, d.Vars[0])
			}
			outf("\nDRY RUN - would ask %s to resolve: %s\n", f.llmModel, strings.Join(names, ", "))
		}
		showSkipped(deferred)
		return nil
	}
	c := e.Client()
	if len(chosen) > 0 {
		var need []string
		for _, a := range chosen {
			if a.Combine != nil {
				need = append(need, a.Combine...)
			} else if a.SecretName != "" {
				need = append(need, a.SecretName)
			}
		}
		values := providers.EnvValues(text, need)
		tools, err := e.nameIDs(c, "/tools")
		if err != nil {
			return err
		}
		secrets, err := e.nameIDs(c, "/secrets")
		if err != nil {
			return err
		}
		ok := 0
		for _, a := range chosen {
			var val string
			if a.Combine != nil {
				u, pw := values[a.Combine[0]], values[a.Combine[1]]
				if u == "" || pw == "" {
					outf("  ✗ %s: %s/%s missing in the env - skipped\n", a.ToolName, a.Combine[0], a.Combine[1])
					continue
				}
				val = base64.StdEncoding.EncodeToString([]byte(u + ":" + pw))
			} else {
				val = values[a.SecretName]
				if val == "" {
					outf("  ✗ %s: %s has no value in the env - skipped\n", a.ToolName, a.SecretName)
					continue
				}
			}
			if id, exists := tools[a.ToolName]; exists {
				if !f.replace {
					outf("  · %s: already registered (use --replace)\n", a.ToolName)
					outf("    ↗ %s\n", e.detailURL("tool", a.ToolName))
					continue
				}
				if err := e.remove(c, idPath("/tools/", id)); err != nil {
					return err
				}
				if sid, ok := secrets[a.SecretName]; ok {
					if err := e.remove(c, idPath("/secrets/", sid)); err != nil {
						return err
					}
				}
			} else if sid, ok := secrets[a.SecretName]; ok && f.replace {
				if err := e.remove(c, idPath("/secrets/", sid)); err != nil {
					return err
				}
			}
			rs, err := e.post(c, "/secrets", pyjson.Ordered("name", a.SecretName, "value", val, "kind", "env"))
			if err != nil {
				return err
			}
			if rs.Status >= 400 {
				outf("  ✗ %s: secret failed (%d) %s\n", a.ToolName, rs.Status, clip(string(rs.Body), 100))
				continue
			}
			sid := secretID(rs.Body)
			bindings := []any{withMember(a.Binding, "secret_id", sid)}
			for _, name := range a.RequiredHeaders.Keys() {
				value, _ := a.RequiredHeaders.Get(name)
				bindings = append(bindings, pyjson.Ordered("secret_id", sid, "injector", "env", "location", "header",
					"name", name, "format", value))
			}
			body := pyjson.Ordered("name", a.ToolName, "base_url", a.BaseURL, "bindings", bindings)
			if a.Health != nil {
				body.Set("health_check", a.Health)
			}
			rt, err := e.post(c, "/tools", body)
			if err != nil {
				return err
			}
			if rt.Status >= 400 {
				outf("  ✗ %s: tool failed (%d) %s\n", a.ToolName, rt.Status, clip(string(rt.Body), 100))
				continue
			}
			outf("  ✓ %-14s %s\n", a.ToolName, a.BaseURL)
			outf("    ↗ %s\n", e.detailURL("tool", a.ToolName))
			ok++
		}
		outf("\nRegistered %d/%d tools.\n", ok, len(chosen))
	}
	if len(oauthDets) > 0 {
		if !f.noOAuth && prompt.StdinIsTerminal() {
			if err := e.oauthLoop(c, oauthDets, text); err != nil {
				return err
			}
		} else {
			var provs []string
			for _, d := range oauthDets {
				provs = append(provs, cmp.Or(d.Provider, "?"))
			}
			outf("\n%d OAuth app(s) detected (%s) - not connected. Connect when ready:  treg connections connect <name>\n",
				len(oauthDets), strings.Join(provs, ", "))
		}
	}
	if f.llm && len(unknowns) > 0 {
		if err := e.uploadLLM(c, unknowns, text, f); err != nil {
			return err
		}
	}
	showSkipped(deferred)
	return nil
}

func showSkipped(skipped []*providers.Action) {
	if len(skipped) == 0 {
		return
	}
	outln("\nNot auto-registered (need another path):")
	for _, a := range skipped {
		outf("  · %s  -  %s\n", strings.Join(a.Detection.Vars, " + "), a.Reason)
	}
}

// oauthLoop connects the detected OAuth apps one at a time, each after a yes on the terminal.
func (e *Env) oauthLoop(c *api.Client, dets []providers.Detection, envText string) error {
	var need []string
	for _, d := range dets {
		cid, csec := providers.OAuthParts(d.Vars)
		for _, v := range []string{cid, csec} {
			if v != "" {
				need = append(need, v)
			}
		}
	}
	vals := providers.EnvValues(envText, need)
	outf("\n%d OAuth provider(s) to connect (each opens a browser consent):\n", len(dets))
	for i, d := range dets {
		ans, err := prompt.Line(fmt.Sprintf("\n  OAuth %d/%d: connect %s (%s)? [y = connect / n = skip / a = skip all]: ",
			i+1, len(dets), d.Provider, strings.Join(d.Vars, " + ")))
		if err != nil {
			ans = "a"
		}
		ans = strings.ToLower(strings.TrimSpace(ans))
		if ans == "a" || ans == "all" || ans == "skip-all" {
			outln("  · skipping all remaining OAuth.")
			break
		}
		if ans != "y" && ans != "yes" {
			outf("  · %s: skipped.\n", d.Provider)
			continue
		}
		if err := e.oauthConnect(c, d, vals); err != nil {
			return err
		}
	}
	return nil
}

func (e *Env) oauthConnect(c *api.Client, d providers.Detection, vals map[string]string) error {
	cid, csec := providers.OAuthParts(d.Vars)
	scopes, ok := d.Auth.Get("scopes")
	if !ok {
		scopes = []any{}
	}
	value := func(k string) any {
		if v, ok := vals[k]; ok {
			return v
		}
		return nil
	}
	body := pyjson.Ordered("name", providers.Slug(d.Provider), "client_id", value(cid), "client_secret", value(csec),
		"auth_uri", d.Auth.String("auth_uri"), "token_uri", d.Auth.String("token_uri"), "scopes", scopes)
	r, err := e.post(c, "/oauth/start", body)
	if err != nil {
		return err
	}
	if r.Status != 200 {
		outf("  ✗ %s: /oauth/start failed (%d) %s\n", d.Provider, r.Status, clip(string(r.Body), 100))
		return nil
	}
	var start map[string]any
	_ = json.Unmarshal(r.Body, &start)
	redirect, rOK := start["redirect_uri"].(string)
	consent, cOK := start["consent_url"].(string)
	state, sOK := start["state"].(string)
	if !rOK || !cOK || !sOK {
		outf("  ✗ %s: unexpected /oauth/start response - skipped\n", d.Provider)
		return nil
	}
	outf("    1. Ensure this redirect URI is allowed in the %s OAuth app:\n       %s\n", d.Provider, redirect)
	outf("    2. Open to authorize:\n       %s\n    Waiting… (Ctrl-C to skip this one)\n", consent)
	interrupt := make(chan os.Signal, 1)
	signal.Notify(interrupt, os.Interrupt)
	defer signal.Stop(interrupt)
	for range 150 {
		select {
		case <-interrupt:
			outf("\n  · %s: skipped (Ctrl-C)\n", d.Provider)
			return nil
		case <-time.After(2 * time.Second):
		}
		s, err := c.Do(api.Request{Path: "/oauth/status/" + state})
		if err != nil {
			continue
		}
		var st map[string]any
		if json.Unmarshal(s.Body, &st) != nil {
			continue
		}
		switch st["status"] {
		case "done":
			if id := str(st["secret_id"]); id != "" {
				outf("  ✓ %s connected (oauth secret id %s)\n", d.Provider, id)
			} else {
				outf("  ✓ %s connected\n", d.Provider)
			}
			return nil
		case "error":
			outf("  ✗ %s: %s\n", d.Provider, cmp.Or(str(st["detail"]), "the connection failed"))
			return nil
		}
	}
	outf("  ✗ %s: timed out waiting for authorization.\n", d.Provider)
	return nil
}
