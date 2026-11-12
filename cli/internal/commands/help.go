package commands

import (
	"fmt"
	"strings"

	"github.com/spf13/cobra"
)

// helpGroups is the front page `treg --help` prints, ordered as the job is done: find a tool, call
// it, see what it cost. A name this build has no command for is left out.
var helpGroups = []struct {
	title string
	rows  [][2]string
}{
	{"THE CATALOG - tools you don't have a key for", [][2]string{
		{"catalog", "Find a tool by what you want to DO. Every endpoint shows its price."},
		{"call", "Call a tool: a catalog endpoint by id, or one of your own by URL."},
		{"host", "Host a reference image / audio / video at a public URL for a vendor to fetch."},
		{"balance", "Prepaid balance: credit left, calls in flight, recent spend."},
		{"topup", "Add funds, or set up automatic top-ups."},
		{"feedback", "Share a problem or suggestion about treg."},
		{"review", "Rate a catalog call after using its result."},
	}},
	{"YOUR OWN TOOLS - what your team already has", [][2]string{
		{"tool", "Manage tools (endpoint or CLI)."},
		{"skill", "Install treg's skill and your team's skills into your coding agents."},
		{"secret", "Manage stored credentials (encrypted server-side, never returned)."},
		{"connections", "Your connected accounts: connect providers, health, expiry."},
		{"mcp", "Register treg's MCP server with your coding agents."},
	}},
	{"BULK UPLOAD", [][2]string{
		{"scan", "Scan a directory / machine (read-only preview of what upload would register)."},
		{"upload", "Upload a directory / machine: .env keys, skills, installed CLIs."},
	}},
	{"TEAM MANAGEMENT", [][2]string{
		{"audit", "Who called/ran what, when, and the result."},
		{"org", "Manage teams (orgs): create, switch, join, members, agent keys."},
		{"invites", "List invites addressed to your email."},
		{"accept", "Accept an invite addressed to your email."},
	}},
	{"CONFIG", [][2]string{
		{"config", "Show or set the registry this CLI talks to."},
		{"login", "Sign in (browser, --email code, or --token for agents/CI)."},
		{"logout", "Clear stored credentials for this machine."},
		{"whoami", "Who this CLI acts as: account, team and role, registry."},
		{"update", "Upgrade the treg CLI in place."},
		{"version", "Print the installed treg version."},
	}},
}

var globalOptions = [][2]string{
	{"-h, --help", "Show this help and exit."},
	{"--version", "Print the treg version and exit."},
	{"--org <slug>", "Run any command in that team instead of the active one."},
	{"--json", "Table commands print raw JSON; `call` prints one {result, _treg} line on stdout."},
}

const rootExamples = "Examples:\n" +
	"  treg login                                              # sign in; first login registers you\n" +
	"  treg catalog search \"backlinks for a domain\"            # find a tool by what it DOES\n" +
	"  treg call tikhub.tiktok.user.profile --query uniqueId=tiktok\n" +
	"  treg balance                                            # what you have, what you spent\n" +
	"  treg upload                                             # register your own .env + skills\n" +
	"\n`treg <command> -h` for details.\n"

// frontPage is the root's help; every other command keeps cobra's own.
func frontPage(root *cobra.Command) func(*cobra.Command, []string) {
	fallback := root.HelpFunc()
	return func(cmd *cobra.Command, args []string) {
		if cmd != root {
			fallback(cmd, args)
			return
		}
		known := map[string]bool{}
		for _, c := range root.Commands() {
			if c.IsAvailableCommand() {
				known[c.Name()] = true
			}
		}
		var b strings.Builder
		b.WriteString("usage: treg [-h] [--version] <command> ...\n\n")
		b.WriteString(rootDescription + "\n")
		for _, g := range helpGroups {
			var lines []string
			for _, r := range g.rows {
				if known[r[0]] {
					lines = append(lines, fmt.Sprintf("    %-16s %s\n", r[0], r[1]))
				}
			}
			if len(lines) > 0 {
				b.WriteString("\n" + g.title + "\n" + strings.Join(lines, ""))
			}
		}
		b.WriteString("\nOPTIONS\n")
		for _, o := range globalOptions {
			fmt.Fprintf(&b, "    %-16s %s\n", o[0], o[1])
		}
		b.WriteString("\n" + rootExamples)
		fmt.Fprint(cmd.OutOrStdout(), b.String())
	}
}
