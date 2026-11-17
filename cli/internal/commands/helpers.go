package commands

import (
	"encoding/json/v2"
	"fmt"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/superdesigndev/treg/cli/internal/api"
)

// quote names a value inside a message, quoted and escaped.
func quote(s string) string { return strconv.Quote(s) }

// nullable is JSON null for an empty string.
func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func str(v any) string {
	switch t := v.(type) {
	case string:
		return t
	case nil:
		return ""
	case float64:
		return strconv.FormatFloat(t, 'f', -1, 64)
	case bool:
		return strconv.FormatBool(t)
	}
	return fmt.Sprint(v)
}

// clip is Python's s[:n], counted in characters.
func clip(s string, n int) string {
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	return string([]rune(s)[:n])
}

var jsonHeader = [][2]string{{"Content-Type", "application/json"}}

type orgRow struct {
	OrgID  int64
	Slug   string
	Role   string
	Active bool
}

// orgRows is your memberships from /orgs, or nil when it will not answer: an agent key is refused
// there on purpose.
func (e *Env) orgRows(c *api.Client) ([]orgRow, error) {
	r, err := e.do(c, api.Request{Path: "/orgs"})
	if err != nil {
		return nil, err
	}
	if r.Status != 200 {
		return nil, nil
	}
	var raw []map[string]any
	_ = json.Unmarshal(r.Body, &raw)
	rows := []orgRow{}
	for _, o := range raw {
		if _, ok := o["slug"]; !ok {
			continue
		}
		id, _ := o["org_id"].(float64)
		if id == 0 {
			id, _ = o["id"].(float64)
		}
		rows = append(rows, orgRow{OrgID: int64(id), Slug: str(o["slug"]), Role: str(o["role"]), Active: o["active"] == true})
	}
	return rows, nil
}

// pickOrg is the membership this CLI acts in: the --org or active slug, else the registry's active one.
func (e *Env) pickOrg(rows []orgRow) *orgRow {
	target := e.EffectiveOrg()
	for i := range rows {
		if rows[i].Slug == target {
			return &rows[i]
		}
	}
	for i := range rows {
		if rows[i].Active {
			return &rows[i]
		}
	}
	return nil
}

func lower(s string) string { return strings.ToLower(s) }
