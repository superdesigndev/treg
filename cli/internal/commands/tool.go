package commands

import (
	"encoding/json"
	"strings"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/output"
	"github.com/superdesigndev/treg/cli/internal/providers"
	"github.com/superdesigndev/treg/cli/internal/pyjson"
)

func toolCmd(env *Env) *cobra.Command {
	cmd := group("tool", "Manage tools (endpoint or CLI).",
		"treg tool ls", "treg tool add stripe --base-url https://api.stripe.com --secret 1")
	cmd.AddCommand(toolAddCmd(env), toolLsCmd(env), toolRmCmd(env), toolUpdateCmd(env))
	return cmd
}

type toolFlags struct {
	baseURL, secret, health, injector, authIn, authName, authFormat, secretField string
	bind, binding                                                                []string
}

func toolAddCmd(env *Env) *cobra.Command {
	var f toolFlags
	cmd := leaf("add <name>", "Register a tool with full control over the credential binding(s).",
		"treg tool add stripe --base-url https://api.stripe.com --secret 1",
		"treg tool add ads --base-url https://api.x.com --bind 'secret=1' --bind 'secret=2,name=developer-token'")
	cmd.Args = cobra.ExactArgs(1)
	fl := cmd.Flags()
	fl.StringVar(&f.baseURL, "base-url", "", "the upstream API root")
	fl.StringVar(&f.secret, "secret", "", "secret id for a single default (Bearer) binding")
	fl.StringArrayVar(&f.bind, "bind", nil, "a binding 'secret=<id>,name=<Header>,format=<fmt>,...' (repeatable)")
	fl.StringArrayVar(&f.binding, "binding", nil, "a raw binding as JSON (repeatable)")
	fl.StringVar(&f.health, "health", "", `a health-check as JSON, e.g. '{"path":"me"}'`)
	fl.StringVar(&f.injector, "injector", "env", "how the secret is injected: env, oauth, secret_file or cli_auth")
	fl.StringVar(&f.authIn, "auth-in", "header", "header | query")
	fl.StringVar(&f.authName, "auth-name", "Authorization", "header/param name")
	fl.StringVar(&f.authFormat, "auth-format", "Bearer {secret}", "injection format")
	fl.StringVar(&f.secretField, "secret-field", "access_token", "JSON field for file/oauth secrets")
	_ = cmd.MarkFlagRequired("base-url")
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		if err := choice("--injector", f.injector, secretKinds...); err != nil {
			return err
		}
		var secretID json.Number
		if cmd.Flags().Changed("secret") {
			id, err := intArg("--secret", f.secret)
			if err != nil {
				return err
			}
			secretID = id
		}
		b := pyjson.Ordered("name", args[0], "base_url", f.baseURL)
		switch {
		case len(f.bind) > 0:
			bindings, err := parseBinds(f.bind)
			if err != nil {
				return err
			}
			b.Set("bindings", bindings)
		case len(f.binding) > 0:
			bindings, err := jsonArgs(f.binding, "binding")
			if err != nil {
				return err
			}
			b.Set("bindings", bindings)
		case secretID != "":
			b.Set("secret_id", secretID)
			b.Set("injector", f.injector)
			b.Set("auth_in", f.authIn)
			b.Set("auth_name", f.authName)
			b.Set("auth_format", f.authFormat)
			b.Set("secret_field", f.secretField)
		}
		if f.health != "" {
			health, err := jsonArg(f.health, "health")
			if err != nil {
				return err
			}
			b.Set("health_check", health)
		}
		return env.show(api.Request{Method: "POST", Path: "/tools", Body: pyBody(b), Header: jsonHeader})
	}
	return cmd
}

func toolLsCmd(env *Env) *cobra.Command {
	cmd := leaf("ls", "List registered tools (names, hosts, bindings).", "treg tool ls")
	cmd.Args = cobra.NoArgs
	cmd.RunE = func(*cobra.Command, []string) error {
		return env.show(api.Request{Path: "/tools"})
	}
	return cmd
}

func toolRmCmd(env *Env) *cobra.Command {
	cmd := leaf("rm <id>", "Delete a tool by id.", "treg tool rm 2")
	cmd.Args = cobra.ExactArgs(1)
	cmd.RunE = func(_ *cobra.Command, args []string) error {
		id, err := intArg("id", args[0])
		if err != nil {
			return err
		}
		return env.show(api.Request{Method: "DELETE", Path: "/tools/" + string(id)})
	}
	return cmd
}

func toolUpdateCmd(env *Env) *cobra.Command {
	var f toolFlags
	var localRun string
	cmd := leaf("update <id>", "Change a tool's base URL, bindings, or health-check.",
		"treg tool update 2 --base-url https://api.stripe.com/v2")
	cmd.Args = cobra.ExactArgs(1)
	fl := cmd.Flags()
	fl.StringVar(&f.baseURL, "base-url", "", "new base URL")
	fl.StringArrayVar(&f.bind, "bind", nil, "replace bindings (repeatable)")
	fl.StringArrayVar(&f.binding, "binding", nil, "replace bindings, raw JSON (repeatable)")
	fl.StringVar(&f.health, "health", "", "new health-check JSON")
	fl.StringVar(&localRun, "local-run", "", "allow/forbid `treg run` (local tier) for this tool (owner opt-in): on or off")
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		id, err := intArg("id", args[0])
		if err != nil {
			return err
		}
		changed := cmd.Flags().Changed
		if changed("local-run") {
			if err := choice("--local-run", localRun, "on", "off"); err != nil {
				return err
			}
		}
		b := pyjson.NewObject()
		if changed("base-url") {
			b.Set("base_url", f.baseURL)
		}
		switch {
		case len(f.bind) > 0:
			bindings, err := parseBinds(f.bind)
			if err != nil {
				return err
			}
			b.Set("bindings", bindings)
		case len(f.binding) > 0:
			bindings, err := jsonArgs(f.binding, "binding")
			if err != nil {
				return err
			}
			b.Set("bindings", bindings)
		}
		if changed("health") {
			health, err := jsonArg(f.health, "health")
			if err != nil {
				return err
			}
			b.Set("health_check", health)
		}
		c := env.Client()
		if localRun != "" {
			// PATCH replaces the whole cli profile, so `enabled` is merged into the current one and
			// flipping it never wipes a contract-declared inject or deny list.
			r, err := env.do(c, api.Request{Path: "/tools"})
			if err != nil {
				return err
			}
			if r.Status != 200 {
				if err := env.Out.Show(r); err != nil {
					return err
				}
			}
			var current *pyjson.Object
			for _, t := range responseList(r) {
				if o, ok := t.(*pyjson.Object); ok && pyjson.Text(o.Value("id")) == string(id) {
					current = o
					break
				}
			}
			if current == nil {
				return output.Fail("tool id %s not found in this org", id)
			}
			cli := pyjson.NewObject()
			if existing, ok := current.Value("cli").(*pyjson.Object); ok {
				for _, k := range existing.Keys() {
					cli.Set(k, existing.Value(k))
				}
			}
			cli.Set("enabled", localRun == "on")
			b.Set("cli", cli)
		}
		if b.Len() == 0 {
			return output.Fail("nothing to update (use --base-url / --bind / --binding / --health / --local-run)")
		}
		r, err := env.do(c, api.Request{Method: "PATCH", Path: "/tools/" + string(id), Body: pyBody(b), Header: jsonHeader})
		if err != nil {
			return err
		}
		return env.Out.Show(r)
	}
	return cmd
}

// parseBinds turns each --bind 'secret=<id>,name=<Header>,...' into a binding with the defaults
// of a Bearer token in Authorization.
func parseBinds(specs []string) ([]any, error) {
	out := make([]any, 0, len(specs))
	for _, spec := range specs {
		b := pyjson.Ordered("injector", "env", "location", "header", "name", "Authorization",
			"format", "Bearer {secret}", "secret_field", "access_token")
		for _, part := range strings.Split(spec, ",") {
			k, v, _ := strings.Cut(part, "=")
			k, v = providers.Strip(k), providers.Strip(v)
			switch _, known := b.Get(k); {
			case k == "secret" || k == "secret_id":
				id, ok := pyInt(v)
				if !ok {
					return nil, output.Fail("--bind secret= must be an integer id, got %s", quote(v))
				}
				b.Set("secret_id", id)
			case known:
				b.Set(k, v)
			default:
				return nil, output.Fail("unknown --bind key %s", quote(k))
			}
		}
		if _, ok := b.Get("secret_id"); !ok {
			return nil, output.Fail("each --bind needs secret=<id>")
		}
		out = append(out, b)
	}
	return out, nil
}

// jsonArg parses an inline-JSON argument, refusing bad JSON with the parser's own message.
func jsonArg(s, label string) (any, error) {
	v, err := pyjson.Loads(s)
	if err != nil {
		return nil, output.Fail("--%s is not valid JSON: %s", label, err)
	}
	return v, nil
}

func jsonArgs(values []string, label string) ([]any, error) {
	out := make([]any, 0, len(values))
	for _, s := range values {
		v, err := jsonArg(s, label)
		if err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, nil
}
