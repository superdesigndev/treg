package commands

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/output"
	"github.com/superdesigndev/treg/cli/internal/providers"
	"github.com/superdesigndev/treg/cli/internal/pyjson"
	"github.com/superdesigndev/treg/cli/internal/skillscan"
)

var secretKinds = []string{"env", "oauth", "secret_file", "cli_auth"}

func secretCmd(env *Env) *cobra.Command {
	cmd := group("secret", "Manage stored credentials (encrypted server-side, never returned).",
		"treg secret add STRIPE_KEY --value sk_live_...", "treg secret ls")
	cmd.AddCommand(secretAddCmd(env), secretLsCmd(env), secretRmCmd(env), secretUpdateCmd(env))
	return cmd
}

type secretFlags struct {
	value, envVar, envFile, file, dir, kind string
}

func secretAddCmd(env *Env) *cobra.Command {
	var f secretFlags
	cmd := leaf("add <name>", "Store a secret (a value, an .env var, a file, or auto-found in a skill dir).",
		"treg secret add STRIPE_KEY --value sk_live_123",
		"treg secret add AHREFS_API_KEY --env-var AHREFS_API_KEY   # read + parse it from ./.env",
		"treg secret add gcp --file creds.json --kind secret_file")
	cmd.Args = cobra.ExactArgs(1)
	fl := cmd.Flags()
	fl.StringVar(&f.value, "value", "", "the secret value inline")
	fl.StringVar(&f.envVar, "env-var", "", "read the value of this variable from an .env (correctly parsed; value stays off the command line)")
	fl.StringVar(&f.envFile, "env-file", "", "the .env to read --env-var from (default: ./.env)")
	fl.StringVar(&f.file, "file", "", "read the value from this file")
	fl.StringVar(&f.dir, "dir", "", "auto-find the secret file in a skill dir")
	fl.StringVar(&f.kind, "kind", "env", "secret kind: env, oauth, secret_file or cli_auth")
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		if err := choice("--kind", f.kind, secretKinds...); err != nil {
			return err
		}
		name := args[0]
		var value, srcFile string
		switch {
		case f.envVar != "":
			envFile := f.envFile
			if envFile == "" {
				envFile = filepath.Join(workingDir(), ".env")
			}
			if !isRegularFile(envFile) {
				return output.Fail("no .env at %s (use --env-file PATH)", envFile)
			}
			text, err := readText(envFile)
			if err != nil {
				return fileError(err, envFile)
			}
			if value = providers.EnvValues(text, []string{f.envVar})[f.envVar]; value == "" {
				return output.Fail("%s not found (or empty) in %s", f.envVar, envFile)
			}
		case f.dir != "":
			found, err := findSecretFile(f.dir, f.kind)
			if err != nil {
				return output.Fail("%s", err)
			}
			srcFile = found
			errln("[using " + srcFile + "]")
			text, err := readText(srcFile)
			if err != nil {
				return output.Fail("%s", err)
			}
			// A trailing newline would become an illegal header value.
			value = providers.Strip(text)
		case f.file != "":
			text, err := readText(f.file)
			if err != nil {
				return fileError(err, f.file)
			}
			value = providers.Strip(text)
		case cmd.Flags().Changed("value"):
			value = f.value
		default:
			return output.Fail("provide --value, --env-var, --file, or --dir")
		}
		c := env.Client()
		r, err := env.do(c, api.Request{Method: "POST", Path: "/secrets",
			Body: jsonBody(pyjson.Ordered("name", name, "value", value, "kind", f.kind)), Header: jsonHeader})
		if err != nil {
			return err
		}
		if r.Status == 200 && f.dir != "" {
			syncContractSecret(f.dir, srcFile, name, f.kind)
		}
		return env.Out.Show(r)
	}
	return cmd
}

// fileError refuses a command whose file could not be read.
func fileError(err error, path string) error {
	return output.Fail("treg: cannot read %s", osError(err, path))
}

func secretLsCmd(env *Env) *cobra.Command {
	cmd := leaf("ls", "List your secrets (names + kinds; never values).", "treg secret ls")
	cmd.Args = cobra.NoArgs
	cmd.RunE = func(*cobra.Command, []string) error {
		return env.show(api.Request{Path: "/secrets"})
	}
	return cmd
}

func secretRmCmd(env *Env) *cobra.Command {
	cmd := leaf("rm <id>", "Delete a secret by id.", "treg secret rm 4")
	cmd.Args = cobra.ExactArgs(1)
	cmd.RunE = func(_ *cobra.Command, args []string) error {
		id, err := idArg("id", args[0])
		if err != nil {
			return err
		}
		return env.show(api.Request{Method: "DELETE", Path: "/secrets/" + id})
	}
	return cmd
}

func secretUpdateCmd(env *Env) *cobra.Command {
	var name, value, kind string
	cmd := leaf("update <id>", "Rename a secret, change its value, or its kind.", "treg secret update 4 --value sk_live_new")
	cmd.Args = cobra.ExactArgs(1)
	fl := cmd.Flags()
	fl.StringVar(&name, "name", "", "new name")
	fl.StringVar(&value, "value", "", "new value")
	fl.StringVar(&kind, "kind", "", "new kind: env, oauth, secret_file or cli_auth")
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		id, err := idArg("id", args[0])
		if err != nil {
			return err
		}
		changed := cmd.Flags().Changed
		if changed("kind") {
			if err := choice("--kind", kind, secretKinds...); err != nil {
				return err
			}
		}
		b := pyjson.NewObject()
		for _, field := range []struct{ key, value string }{{"name", name}, {"value", value}, {"kind", kind}} {
			if changed(field.key) {
				b.Set(field.key, field.value)
			}
		}
		if b.Len() == 0 {
			return output.Fail("nothing to update (use --name / --value / --kind)")
		}
		return env.show(api.Request{Method: "PATCH", Path: "/secrets/" + id, Body: jsonBody(b), Header: jsonHeader})
	}
	return cmd
}

// show sends one request and prints the registry's answer.
func (e *Env) show(req api.Request) error {
	r, err := e.do(e.Client(), req)
	if err != nil {
		return err
	}
	return e.Out.Show(r)
}

// contractFile is the treg.json a skill can carry so treg registers it.
const contractFile = "treg.json"

// findSecretFile is the one secret file of a kind in a skill dir's .secret/.secrets: none or
// several is an error naming what was found.
func findSecretFile(dir, kind string) (string, error) {
	candidates := skillscan.SecretFiles(dir)
	if len(candidates) == 0 {
		return "", fmt.Errorf("no .secret/.secrets files under %s", dir)
	}
	var matches []string
	for _, f := range candidates {
		if matchesKind(f, kind) {
			matches = append(matches, f)
		}
	}
	if len(matches) == 0 {
		return "", fmt.Errorf("no %s secret in %s (found: %s)", quote(kind), dir, strings.Join(baseNames(candidates), ", "))
	}
	if len(matches) > 1 {
		return "", fmt.Errorf("ambiguous %s secret in %s: %s - use --file", quote(kind), dir, strings.Join(baseNames(matches), ", "))
	}
	return matches[0], nil
}

func baseNames(paths []string) []string {
	out := make([]string, len(paths))
	for i, p := range paths {
		out[i] = filepath.Base(p)
	}
	return out
}

// matchesKind: oauth and secret_file are a JSON token blob (access_token, token or refresh_token);
// env and cli_auth are a file that is not JSON. A client_secret.json is therefore never oauth.
func matchesKind(path, kind string) bool {
	if !strings.HasSuffix(path, ".json") {
		return kind == "env" || kind == "cli_auth"
	}
	if kind != "oauth" && kind != "secret_file" {
		return false
	}
	text, err := skillscan.ReadText(path)
	if err != nil {
		return false
	}
	data, _ := pyjson.Loads(text)
	o, ok := data.(*pyjson.Object)
	if !ok {
		return false
	}
	for _, k := range []string{"access_token", "token", "refresh_token"} {
		if _, ok := o.Get(k); ok {
			return true
		}
	}
	return false
}

// syncContractSecret records the secret in the skill's treg.json, so the contract follows what the
// CLI registered. It runs after the secret exists server-side, so a failure is only a warning.
func syncContractSecret(dir, srcFile, name, kind string) {
	path := filepath.Join(dir, contractFile)
	if !exists(path) || srcFile == "" {
		return
	}
	if err := updateContract(path, dir, srcFile, name, kind); err != nil {
		errln(fmt.Sprintf("[warning: could not sync %s: %s]", contractFile, err))
		return
	}
	errln("[synced " + contractFile + "]")
}

func updateContract(path, dir, srcFile, name, kind string) error {
	text, err := readText(path)
	if err != nil {
		if _, ok := errors.AsType[*os.PathError](err); ok {
			return errors.New(osError(err, path))
		}
		return err
	}
	parsed, err := pyjson.Loads(text)
	if err != nil {
		return err
	}
	contract, ok := parsed.(*pyjson.Object)
	if !ok {
		return fmt.Errorf("%s is not a JSON object", contractFile)
	}
	rel, err := relativeTo(srcFile, dir)
	if err != nil {
		return err
	}
	if _, ok := contract.Get("secrets"); !ok {
		contract.Set("secrets", []any{})
	}
	secrets, ok := contract.Value("secrets").([]any)
	if !ok {
		return errors.New("its secrets is not a list")
	}
	var entry *pyjson.Object
	for _, s := range secrets {
		o, ok := s.(*pyjson.Object)
		if !ok {
			return errors.New("a secrets entry is not an object")
		}
		if file, ok := o.Value("file").(string); ok && filepath.ToSlash(file) == rel {
			entry = o
			break
		}
	}
	if entry == nil {
		contract.Set("secrets", append(secrets, pyjson.Ordered("file", rel, "name", name, "kind", kind)))
	} else {
		entry.Set("name", name)
		entry.Set("kind", kind)
	}
	if err := os.WriteFile(path, []byte(pyjson.Dumps(contract, pyjson.Pretty)), 0o666); err != nil {
		return errors.New(osError(err, path))
	}
	return nil
}

// relativeTo is path relative to base, both with every symlink followed, with "/" between its parts
// so a treg.json reads the same on every system; a path outside base is an error.
func relativeTo(path, base string) (string, error) {
	p, err := resolve(path)
	if err != nil {
		return "", err
	}
	b, err := resolve(base)
	if err != nil {
		return "", err
	}
	rel, err := filepath.Rel(b, p)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("%s is not inside %s", quote(p), quote(b))
	}
	return filepath.ToSlash(rel), nil
}

// resolve is the absolute path with every symlink followed.
func resolve(path string) (string, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	if real, err := filepath.EvalSymlinks(abs); err == nil {
		return real, nil
	}
	return abs, nil
}
