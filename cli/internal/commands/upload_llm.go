package commands

import (
	"cmp"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/prompt"
	"github.com/superdesigndev/treg/cli/internal/providers"
	"github.com/superdesigndev/treg/cli/internal/pyjson"
)

var llmHTTP = &http.Client{Timeout: 60 * time.Second}

// llmChat is one OpenAI-compatible chat completion, the provider's own key as the bearer.
func llmChat(base, token, model, system, user string) (string, error) {
	body := pyjson.Ordered("model", model, "temperature", 0, "messages", []any{
		pyjson.Ordered("role", "system", "content", system), pyjson.Ordered("role", "user", "content", user)})
	req, err := http.NewRequest(http.MethodPost, strings.TrimRight(base, "/")+"/chat/completions",
		strings.NewReader(pyjson.Dumps(body, pyjson.Body)))
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := llmHTTP.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return "", fmt.Errorf("HTTP %d from %s", resp.StatusCode, req.URL)
	}
	var out struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return "", err
	}
	if len(out.Choices) == 0 {
		return "", fmt.Errorf("the reply has no choices")
	}
	return out.Choices[0].Message.Content, nil
}

// uploadLLM asks a model for the unknown keys' providers, then registers each only after a yes.
func (e *Env) uploadLLM(c *api.Client, unknowns []providers.Detection, envText string, f uploadFlags) error {
	token := cmp.Or(f.llmToken, os.Getenv("TREG_LLM_TOKEN"))
	if token == "" {
		outln("\n[--llm needs an API token: pass --llm-token <key> or set TREG_LLM_TOKEN]")
		return nil
	}
	var names []string
	for _, d := range unknowns {
		names = append(names, d.Vars[0])
	}
	system, user := providers.LLMPrompt(names)
	outf("\nAsking %s to resolve %d unknown key(s)…\n", f.llmModel, len(names))
	text, err := llmChat(f.llmBaseURL, token, f.llmModel, system, user)
	if err != nil {
		outf("  ✗ LLM call failed: %v\n", err)
		return nil
	}
	resolvedRows := providers.LLMParse(text)
	if len(resolvedRows) == 0 {
		outln("  · the LLM returned no confident matches.")
		return nil
	}
	vals := providers.EnvValues(envText, names)
	tools, err := e.nameIDs(c, "/tools")
	if err != nil {
		return err
	}
	secrets, err := e.nameIDs(c, "/secrets")
	if err != nil {
		return err
	}
	used := map[string]bool{}
	for k := range tools {
		used[k] = true
	}
	for _, row := range resolvedRows {
		v, base, auth := row.String("var"), row.String("base_url"), providers.Obj(row, "auth")
		binding := providers.BuildBinding(auth)
		if binding == nil {
			outf("  · %s: unsupported auth shape %s - skipped\n", v, auth.String("shape"))
			continue
		}
		hdr := ""
		if auth.String("shape") != "bearer" {
			hdr = " " + cmp.Or(cmp.Or(auth.String("header"), auth.String("param")), "-")
		}
		ans := "n"
		question := fmt.Sprintf("  LLM: %s → %s %s  [%s%s]. Register? [y/N]: ", v, cmp.Or(row.String("provider"), "-"), base, auth.String("shape"), hdr)
		if prompt.StdinIsTerminal() {
			if line, err := prompt.Line(question); err == nil {
				ans = line
			}
		} else {
			outf("%s", question)
		}
		ans = strings.ToLower(strings.TrimSpace(ans))
		if ans != "y" && ans != "yes" {
			outf("  · %s: skipped.\n", v)
			continue
		}
		val := vals[v]
		if val == "" {
			outf("  ✗ %s: no value in the env - skipped\n", v)
			continue
		}
		slug := providers.Slug(cmp.Or(row.String("provider"), v))
		toolName, n := slug, 2
		for used[toolName] {
			if _, existing := tools[toolName]; f.replace && existing {
				break
			}
			toolName = fmt.Sprintf("%s-%d", slug, n)
			n++
		}
		if sid, ok := secrets[v]; f.replace && ok {
			if id, ok := tools[toolName]; ok && pyjson.Truthy(id) {
				if err := e.remove(c, idPath("/tools/", id)); err != nil {
					return err
				}
			}
			if err := e.remove(c, idPath("/secrets/", sid)); err != nil {
				return err
			}
		} else if ok {
			outf("  · %s: already registered (use --replace)\n", v)
			continue
		}
		rs, err := e.post(c, "/secrets", pyjson.Ordered("name", v, "value", val, "kind", "env"))
		if err != nil {
			return err
		}
		if rs.Status >= 400 {
			outf("  ✗ %s: secret failed (%d) %s\n", v, rs.Status, clip(string(rs.Body), 80))
			continue
		}
		sid := secretID(rs.Body)
		rt, err := e.post(c, "/tools", pyjson.Ordered("name", toolName, "base_url", base, "bindings", []any{withMember(binding, "secret_id", sid)}))
		if err != nil {
			return err
		}
		if rt.Status < 400 {
			used[toolName] = true
			outf("  ✓ %s %s\n", toolName, base)
		} else {
			outf("  ✗ %s: tool failed (%d) %s\n", v, rt.Status, clip(string(rt.Body), 80))
		}
	}
	return nil
}
