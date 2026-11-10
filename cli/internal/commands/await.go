package commands

import (
	"bytes"
	"encoding/json/jsontext"
	"encoding/json/v2"
	"fmt"
	"math"
	"net/url"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"time"

	"github.com/superdesigndev/treg/cli/internal/api"
	"github.com/superdesigndev/treg/cli/internal/output"
)

// awaitedTask is the registry's answer about an async call's task (`/tasks/{call_id}`). The
// registry reads the catalog's async descriptor; this client only follows the answer.
type awaitedTask struct {
	State       string `json:"state"`
	TaskShown   string `json:"task_shown"`
	Status      string `json:"status"`
	StatusShown string `json:"status_shown"`
	Warning     string `json:"warning"`
	Response    *struct {
		HTTPStatus int    `json:"http_status"`
		Body       string `json:"body"`
	} `json:"response"`
	ResultText   string  `json:"result_text"`
	FetchCommand string  `json:"fetch_command"`
	TTLNote      string  `json:"ttl_note"`
	Resume       string  `json:"resume"`
	RetryAfter   float64 `json:"retry_after"`
	MaxWait      float64 `json:"max_wait"`
	Error        *struct {
		Kind       string `json:"kind"`
		Message    string `json:"message"`
		HTTPStatus int    `json:"http_status"`
		Retryable  bool   `json:"retryable"`
	} `json:"error"`
}

const (
	awaitPollFailures   = 5
	awaitDefaultWait    = 10.0
	awaitBackoffCeilS   = 60.0
	awaitTimedOut       = "timed out while waiting for the async task"
	awaitNoTaskEndpoint = "treg: this registry has no /tasks/ endpoint, which --await needs (a registry newer " +
		"than treg 0.30.0 has it); ask its operator to update it"
)

// taskWaiter follows one task until it is terminal, the wait runs out or the user interrupts it.
type taskWaiter struct {
	env    *Env
	client *api.Client
	path   string
	resume string
	sig    chan os.Signal
}

type interrupted struct{}

func (interrupted) Error() string { return "interrupted" }

// awaitTask follows an async submission through the registry's task endpoint. Stdout carries only
// the terminal answer; every line treg adds goes to stderr.
func (e *Env) awaitTask(c *api.Client, sub *api.Response, timeout float64) error {
	if sub.Status >= 400 {
		return e.Out.Show(sub)
	}
	callID := sub.Header.Get("X-Treg-Call-Id")
	if callID == "" {
		return &output.Exit{Code: 1, Message: awaitNoTaskEndpoint}
	}
	w := &taskWaiter{env: e, client: c, path: "/tasks/" + url.PathEscape(callID), sig: make(chan os.Signal, 1)}
	signal.Notify(w.sig, os.Interrupt)
	defer signal.Stop(w.sig)
	err := w.follow(sub, timeout)
	if _, ok := err.(interrupted); ok {
		msg := "treg: waiting interrupted; the upstream task is still recoverable"
		if w.resume != "" {
			msg += "\nresume: " + w.resume
		}
		return &output.Exit{Code: 3, Message: msg}
	}
	return err
}

func (w *taskWaiter) follow(sub *api.Response, timeout float64) error {
	first, err := w.describe(sub)
	if err != nil {
		return err
	}
	stderr := w.env.Out.Err
	w.resume = first.Resume
	if first.TaskShown != "" {
		fmt.Fprintf(stderr, "async task submitted: %s\n", first.TaskShown)
		if first.Resume != "" {
			fmt.Fprintf(stderr, "resume: %s\n", first.Resume)
		}
	}
	if reserved := strings.TrimSpace(sub.Header.Get("X-Treg-Cost-Micro")); reserved != "" {
		if micro, err := strconv.ParseInt(reserved, 10, 64); err == nil {
			fmt.Fprintf(stderr, "generation reservation: %s\n", output.USD(micro))
		}
	}
	if first.State != "pending" {
		return w.finish(first)
	}
	return w.wait(first, timeout)
}

// describe is the first look: the registry reads the submission itself and calls no provider.
func (w *taskWaiter) describe(sub *api.Response) (*awaitedTask, error) {
	resp, err := w.send(api.Request{Method: "POST", Path: w.path, Body: sub.Body,
		Header: [][2]string{{"Content-Type", "application/octet-stream"}}})
	if err != nil {
		if _, ok := err.(interrupted); ok {
			return nil, err
		}
		return nil, &output.Exit{Code: 3, Message: w.env.unreachable(err).Error() +
			"; the upstream task is still recoverable"}
	}
	t, ok := decodeTask(resp)
	refusal := taskRefusal(resp)
	switch {
	case resp.Status == 200 && ok:
		return t, nil
	case refusal != "":
		return nil, output.Fail("treg: %s", refusal)
	case resp.Status == 404 || resp.Status == 405 || resp.Status < 400:
		return nil, &output.Exit{Code: 1, Message: awaitNoTaskEndpoint}
	case resp.Status >= 500:
		return nil, &output.Exit{Code: 3, Message: fmt.Sprintf(
			"treg: the task endpoint answered HTTP %d; the upstream task is still recoverable", resp.Status)}
	}
	return nil, output.Fail("treg: the task endpoint answered HTTP %d", resp.Status)
}

func (w *taskWaiter) wait(t *awaitedTask, timeout float64) error {
	stderr := w.env.Out.Err
	start := time.Now()
	limit := timeout
	if t.MaxWait > 0 && t.MaxWait < limit {
		limit = t.MaxWait
	}
	interval := t.RetryAfter
	if interval <= 0 {
		interval = awaitDefaultWait
	}
	failures := 0
	warned := map[string]bool{}
	retry := func(cause string) {
		failures++
		fmt.Fprintf(stderr, "async poll retry %d/%d after %s\n", failures, awaitPollFailures, cause)
	}
	for {
		if time.Since(start).Seconds() >= limit {
			return stop(3, awaitTimedOut)
		}
		pause := interval
		if failures > 0 {
			pause = math.Min(awaitBackoffCeilS, interval*math.Pow(2, float64(failures-1)))
		}
		if err := w.sleep(pause); err != nil {
			return err
		}
		resp, err := w.send(api.Request{Path: w.path})
		if err != nil {
			if _, ok := err.(interrupted); ok {
				return err
			}
			retry("a network error: " + err.Error())
			if failures >= awaitPollFailures {
				return stop(3, "polling failed five consecutive times: "+err.Error())
			}
			continue
		}
		if resp.Status >= 500 {
			retry(fmt.Sprintf("HTTP %d", resp.Status))
			if failures >= awaitPollFailures {
				return stop(3, fmt.Sprintf("polling returned %d five consecutive times", resp.Status))
			}
			continue
		}
		next, ok := decodeTask(resp)
		if resp.Status >= 400 || !ok {
			return stop(3, fmt.Sprintf("polling returned HTTP %d", resp.Status))
		}
		if next.Response != nil && next.Response.HTTPStatus > 0 {
			w.env.Note("status_class", fmt.Sprintf("%dxx", next.Response.HTTPStatus/100))
		}
		if next.RetryAfter > 0 {
			interval = next.RetryAfter
		}
		switch {
		case next.State == "pending":
			failures = 0
			if next.Warning != "" && !warned[next.Status] {
				warned[next.Status] = true
				fmt.Fprintf(stderr, "warning: %s\n", next.Warning)
			}
			fmt.Fprintf(stderr, "async task %s: %s (%ds elapsed)\n", next.TaskShown, next.StatusShown,
				int(time.Since(start).Seconds()))
		case next.State == "error" && next.Error != nil && next.Error.Kind == "poll_failed":
			if !next.Error.Retryable {
				return stop(3, next.Error.Message)
			}
			retry(fmt.Sprintf("HTTP %d", next.Error.HTTPStatus))
			if failures >= awaitPollFailures {
				return stop(3, fmt.Sprintf("polling returned %d five consecutive times", next.Error.HTTPStatus))
			}
		default:
			return w.finish(next)
		}
	}
}

// finish prints a final answer: the terminal response on stdout, what to retrieve on stderr.
func (w *taskWaiter) finish(t *awaitedTask) error {
	out, stderr := w.env.Out.Out, w.env.Out.Err
	code := 0
	switch {
	case t.State == "failed":
		code = 2
	case t.Error != nil:
		code = taskErrorCode(t.Error.Kind)
	case t.State != "succeeded":
		return stop(1, "the task endpoint answered an unknown state "+strconv.Quote(t.State))
	}
	// A look that could not read a terminal answer leaves stdout empty.
	if t.Response != nil && code != 3 {
		out.Write([]byte(t.Response.Body))
	}
	if t.ResultText != "" {
		fmt.Fprintf(stderr, "result: %s\n", t.ResultText)
	}
	if t.FetchCommand != "" {
		fmt.Fprintf(stderr, "retrieve the result (file bytes, or JSON with a download URL): %s\n", t.FetchCommand)
	}
	if t.TTLNote != "" {
		fmt.Fprintf(stderr, "download promptly; result lifetime: %s\n", t.TTLNote)
	}
	switch {
	case t.Error != nil:
		return stop(code, t.Error.Message)
	case code != 0:
		return &output.Exit{Code: code}
	}
	return nil
}

// taskErrorCode: a submission or result treg cannot read is malformed metadata (1); anything else
// leaves the task running and recoverable (3).
func taskErrorCode(kind string) int {
	switch kind {
	case "submission_not_json", "submission_rejected", "result_not_retrievable":
		return 1
	}
	return 3
}

func stop(code int, message string) error {
	return &output.Exit{Code: code, Message: "treg: " + message}
}

// send is one registry request that the user's Ctrl-C abandons.
func (w *taskWaiter) send(r api.Request) (*api.Response, error) {
	type answer struct {
		resp *api.Response
		err  error
	}
	done := make(chan answer, 1)
	go func() {
		resp, err := w.client.Do(r)
		done <- answer{resp, err}
	}()
	select {
	case a := <-done:
		return a.resp, a.err
	case <-w.sig:
		return nil, interrupted{}
	}
}

func (w *taskWaiter) sleep(seconds float64) error {
	timer := time.NewTimer(time.Duration(seconds * float64(time.Second)))
	defer timer.Stop()
	select {
	case <-timer.C:
		return nil
	case <-w.sig:
		return interrupted{}
	}
}

// decodeTask reads a task answer; false when the body is not one, as from a registry without the
// task endpoint.
func decodeTask(resp *api.Response) (*awaitedTask, bool) {
	var raw map[string]jsontext.Value
	if json.Unmarshal(bytes.TrimSpace(resp.Body), &raw) != nil {
		return nil, false
	}
	if _, ok := raw["state"]; !ok {
		return nil, false
	}
	var t awaitedTask
	if json.Unmarshal(resp.Body, &t) != nil {
		return nil, false
	}
	return &t, true
}

// taskRefusal is the message of the task endpoint's own refusal (unknown call id, a body that is
// not the relayed answer), or "" for any other answer.
func taskRefusal(resp *api.Response) string {
	var body struct {
		Detail struct {
			Error   string `json:"error"`
			Message string `json:"message"`
		} `json:"detail"`
	}
	if resp.Status < 400 || json.Unmarshal(resp.Body, &body) != nil || body.Detail.Error == "" {
		return ""
	}
	return body.Detail.Message
}
