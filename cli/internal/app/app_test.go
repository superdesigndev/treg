package app

import (
	"reflect"
	"testing"
)

func TestPopOrgKeepsTheSlugAndTheRest(t *testing.T) {
	for _, c := range []struct {
		in   []string
		org  string
		rest []string
	}{
		{[]string{"--org", "beta-team", "org", "ls"}, "beta-team", []string{"org", "ls"}},
		{[]string{"call", "x", "--org=acme"}, "acme", []string{"call", "x"}},
		{[]string{"whoami"}, "", []string{"whoami"}},
	} {
		org, rest, err := popOrg(append([]string(nil), c.in...))
		if err != nil || org != c.org || !reflect.DeepEqual(rest, c.rest) {
			t.Errorf("popOrg(%v) = %q %v %v", c.in, org, rest, err)
		}
	}
	if _, _, err := popOrg([]string{"call", "--org"}); err == nil {
		t.Error("a trailing --org was accepted")
	}
}
