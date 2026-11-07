package commands

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"sort"
	"strconv"
	"strings"

	"github.com/spf13/cobra"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/output"
	"github.com/superdesigndev/treg/cli/internal/prompt"
	"github.com/superdesigndev/treg/cli/internal/pyjson"
	"github.com/superdesigndev/treg/cli/internal/term"
)

type topupFlags struct {
	auto, threshold, amount, cap, perHour string
}

func topupCmd(env *Env) *cobra.Command {
	var f topupFlags
	cmd := leaf("topup [amount]", "Add funds to your team's balance, or set up automatic top-ups.",
		"treg topup                                     # a Checkout link for the default amount",
		"treg topup 100                                 # ...for $100 (+10% bonus credit)",
		"treg topup --auto on --threshold 5 --amount 20 # refill $20 whenever it drops below $5",
		"treg topup --auto off")
	cmd.Short = "Add funds, or set up automatic top-ups."
	cmd.Args = cobra.MaximumNArgs(1)
	fl := cmd.Flags()
	fl.StringVar(&f.auto, "auto", "", "turn automatic top-ups on or off")
	fl.StringVar(&f.threshold, "threshold", "", "with --auto on: refill when the balance drops below this many dollars")
	fl.StringVar(&f.amount, "amount", "", "with --auto on: how many dollars to add on each automatic refill")
	fl.StringVar(&f.cap, "cap", "", "with --auto on: the most auto top-up may charge in a calendar month (default 2000; 0 restores the default)")
	fl.StringVar(&f.perHour, "per-hour", "", "with --auto on: how many automatic refills may run in one hour (1-60, default 5)")
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		changed := cmd.Flags().Changed
		var amount *float64
		if len(args) == 1 {
			v, err := floatArg("amount", args[0])
			if err != nil {
				return err
			}
			amount = &v
		}
		auto := topupAuto{}
		if changed("auto") {
			if err := choice("--auto", f.auto, "on", "off"); err != nil {
				return err
			}
		}
		for _, x := range []struct {
			flag, value string
			into        **float64
		}{{"threshold", f.threshold, &auto.threshold}, {"amount", f.amount, &auto.amount}, {"cap", f.cap, &auto.cap}} {
			if changed(x.flag) {
				v, err := floatArg("--"+x.flag, x.value)
				if err != nil {
					return err
				}
				*x.into = &v
			}
		}
		if changed("per-hour") {
			n, err := intArg("--per-hour", f.perHour)
			if err != nil {
				return err
			}
			auto.perHour = n
		}
		if f.auto != "" {
			return env.topupAuto(f.auto == "on", auto)
		}
		return env.topup(amount)
	}
	return cmd
}

// topup prints a Stripe Checkout link. Payment happens on Stripe's page and the balance moves when
// Stripe tells the registry, so there is nothing to poll here.
func (e *Env) topup(amount *float64) error {
	b := pyjson.NewObject()
	if amount != nil {
		b.Set("amount_usd", float64(*amount))
	}
	r, err := e.do(e.Client(), api.Request{Method: "POST", Path: "/billing/topup", Body: pyBody(b), Header: jsonHeader})
	if err != nil {
		return err
	}
	if e.JSON || r.Status >= 400 {
		return e.Out.Show(r)
	}
	out := responseObject(r)
	p := e.Colors()
	micro := out.Value("amount_micro")
	outf("\n  %sAdd %s to your balance%s\n", p.Accent, usd(micro), p.Reset)
	if tiers := e.bonusTiers(); len(tiers) > 0 {
		amountMicro, _ := pyIntOf(micro)
		dollars := floorDiv(amountMicro, 1_000_000)
		pct, found := int64(0), false
		for _, t := range tiers {
			if dollars >= t[0] && (!found || t[1] > pct) {
				pct, found = t[1], true
			}
		}
		if pct != 0 {
			outf("  %s+%d%% bonus credit%s (%s) added when it lands\n", p.Green, pct, p.Reset, usd(floorDiv(amountMicro*pct, 100)))
		}
		sort.SliceStable(tiers, func(i, j int) bool { return tiers[i][0] < tiers[j][0] })
		for _, t := range tiers {
			if t[0] > dollars && t[1] > pct {
				outf("  %stop up $%d or more for +%d%% bonus credit%s\n", p.Muted, t[0], t[1], p.Reset)
				break
			}
		}
	}
	outf("\n  Pay on Stripe's secure page:\n\n    %s%s%s\n", p.Teal, pyjson.Text(out.Value("url")), p.Reset)
	outf("\n  %sYour balance updates as soon as Stripe confirms the payment "+
		"(seconds). Check it with `treg balance`.%s\n\n", p.Muted, p.Reset)
	return nil
}

// bonusTiers is [min_usd, percent] pairs from GET /billing, or nil when they cannot be read; they
// only decorate the top-up output.
func (e *Env) bonusTiers() [][2]int64 {
	r, err := e.Client().Do(api.Request{Path: "/billing"})
	if err != nil || r.Status >= 400 {
		return nil
	}
	topup, _ := responseObject(r).Value("topup").(*pyjson.Object)
	raw, _ := topup.Value("bonus_tiers").(*pyjson.Object)
	var tiers [][2]int64
	for _, k := range raw.Keys() {
		min, ok1 := pyIntOf(k)
		pct, ok2 := pyIntOf(raw.Value(k))
		if !ok1 || !ok2 {
			return nil
		}
		tiers = append(tiers, [2]int64{min, pct})
	}
	return tiers
}

type topupAuto struct {
	threshold, amount, cap *float64
	perHour                json.Number
}

// topupAuto sets automatic top-ups. An off-session charge needs the cardholder's agreement to the
// threshold and amount, so turning it on asks the person at the terminal first, and a run with no
// terminal counts as no.
func (e *Env) topupAuto(on bool, a topupAuto) error {
	b := pyjson.Ordered("enabled", on, "consent", false)
	for _, x := range []struct {
		key string
		v   *float64
	}{{"threshold_usd", a.threshold}, {"amount_usd", a.amount}, {"monthly_cap_usd", a.cap}} {
		if x.v != nil {
			b.Set(x.key, float64(*x.v))
		}
	}
	if a.perHour != "" {
		b.Set("per_hour", a.perHour)
	}
	p := e.Colors()
	if on {
		threshold, amount := "your threshold", "the default amount"
		if a.threshold != nil {
			threshold = fmt.Sprintf("$%.2f", *a.threshold)
		}
		if a.amount != nil {
			amount = fmt.Sprintf("$%.2f", *a.amount)
		}
		perHour := "up to 5"
		if a.perHour != "" && a.perHour != "0" {
			perHour = string(a.perHour)
		}
		outf("\n  %sAuto top-up charges your saved card when nobody is at the keyboard.%s\n", p.Amber, p.Reset)
		outf("  %sWhenever your balance drops below %s, we charge %s to the card on file, "+
			"%s times per hour at most.%s\n\n", p.Muted, threshold, amount, perHour, p.Reset)
		if !confirmConsent() {
			return output.Fail("cancelled - auto top-up is unchanged.")
		}
		b.Set("consent", true)
	}
	r, err := e.do(e.Client(), api.Request{Method: "POST", Path: "/billing/autotopup", Body: pyBody(b), Header: jsonHeader})
	if err != nil {
		return err
	}
	if e.JSON || r.Status >= 400 {
		return e.Out.Show(r)
	}
	state := responseObject(r)
	auto, _ := state.Value("autotopup").(*pyjson.Object)
	if !on {
		outf("\n  %sAuto top-up is off.%s Add funds by hand with `treg topup`.\n\n", p.Green, p.Reset)
		return nil
	}
	if setup := state.Value("setup_url"); pyjson.Truthy(setup) {
		// Consent is recorded before any card exists, which is the record a dispute turns on.
		outf("\n  %sOne step left: save a card.%s\n", p.Accent, p.Reset)
		outf("\n    %s%s%s\n", p.Teal, pyjson.Text(setup), p.Reset)
		outf("\n  %sAuto top-up switches on by itself once the card is saved.%s\n\n", p.Muted, p.Reset)
		return nil
	}
	outf("\n  %sAuto top-up is on.%s Add %s whenever the balance drops below %s, at most %s times per hour.\n",
		p.Green, p.Reset, usd(orZero(auto.Value("amount_micro"))), usd(orZero(auto.Value("threshold_micro"))),
		pyjson.Text(orDefault(auto.Value("per_hour"), "5")))
	outf("  %sMonthly ceiling %s - we stop there, whatever happens.%s\n\n", p.Muted, usd(orZero(auto.Value("monthly_cap_micro"))), p.Reset)
	return nil
}

func orZero(v any) any {
	if pyjson.Truthy(v) {
		return v
	}
	return int64(0)
}

// confirmConsent is an explicit yes to unattended charges. It defaults to no, and without a
// terminal it is no: consent a script gives unasked is not consent.
func confirmConsent() bool {
	if !term.IsTerminal(os.Stdin) {
		return false
	}
	yes, err := prompt.Confirm("Authorize treg to charge your saved card automatically?")
	return err == nil && yes
}

// usd is a micro-USD amount as money, the sign outside the dollar mark. Under a dollar it keeps four
// decimals: a catalog call costs fractions of a cent, and two decimals would print it as free.
func usd(micro any) string {
	n, ok := pyIntOf(micro)
	if !ok {
		return "-"
	}
	v := float64(n) / 1_000_000
	sign := ""
	if v < 0 {
		sign, v = "-", -v
	}
	if v >= 1 {
		return sign + "$" + groupThousands(strconv.FormatFloat(v, 'f', 2, 64))
	}
	return sign + "$" + strconv.FormatFloat(v, 'f', 4, 64)
}

func groupThousands(s string) string {
	whole, frac, _ := strings.Cut(s, ".")
	var b strings.Builder
	for i, c := range whole {
		if i > 0 && (len(whole)-i)%3 == 0 {
			b.WriteByte(',')
		}
		b.WriteRune(c)
	}
	return b.String() + "." + frac
}

// pyIntOf is Python's int() of a decoded JSON value: an int, a float truncated, a bool, or a
// string of digits.
func pyIntOf(v any) (int64, bool) {
	switch t := v.(type) {
	case int64:
		return t, true
	case int:
		return int64(t), true
	case bool:
		if t {
			return 1, true
		}
		return 0, true
	case json.Number:
		if !strings.ContainsAny(string(t), ".eE") {
			n, err := strconv.ParseInt(string(t), 10, 64)
			return n, err == nil
		}
		f, _ := strconv.ParseFloat(string(t), 64)
		return truncate(f)
	case float64:
		return truncate(t)
	case string:
		n, ok := pyInt(t)
		if !ok {
			return 0, false
		}
		v, err := strconv.ParseInt(string(n), 10, 64)
		return v, err == nil
	}
	return 0, false
}

func truncate(f float64) (int64, bool) {
	if math.IsNaN(f) || math.IsInf(f, 0) {
		return 0, false
	}
	return int64(f), true
}

// floorDiv is Python's a // b.
func floorDiv(a, b int64) int64 {
	q := a / b
	if (a%b != 0) && ((a < 0) != (b < 0)) {
		q--
	}
	return q
}
