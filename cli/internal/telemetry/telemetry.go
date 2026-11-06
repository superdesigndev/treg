// Package telemetry records the CLI's anonymous usage events in a local queue and sends them to the
// registry's relay during a later command, so no command waits on analytics and none is lost to a
// process that exits first. Only the fixed vocabulary in docs/context/interface/cli.md is recorded:
// never an argument, query, body, path, token, email or team identifier.
package telemetry

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"time"

	"github.com/superdesigndev/treg/cli/internal/buildinfo"
	"github.com/superdesigndev/treg/cli/internal/config"
	"github.com/superdesigndev/treg/cli/internal/session"
)

const (
	queueName  = "telemetry-queue.jsonl"
	maxQueue   = 256 * 1024
	batchSize  = 50
	orphanAge  = time.Minute
	sendBudget = 10 * time.Second
)

type Event struct {
	Event      string         `json:"event"`
	DistinctID string         `json:"distinct_id"`
	UUID       string         `json:"uuid"`
	Timestamp  string         `json:"timestamp"`
	Properties map[string]any `json:"properties"`
}

// Enabled is false under TREG_TELEMETRY=0|false|off or DO_NOT_TRACK=1: then nothing is written,
// sent, or read from the process tree.
func Enabled() bool {
	switch strings.ToLower(os.Getenv("TREG_TELEMETRY")) {
	case "0", "false", "off":
		return false
	}
	return os.Getenv("DO_NOT_TRACK") != "1"
}

var uuidShape = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

// InstallationID is the random id in analytics-id beside the config, the one the installer and
// the Python CLI use, independent of login.
func InstallationID() string {
	path := config.Sibling("analytics-id")
	if raw, err := os.ReadFile(path); err == nil {
		id := strings.ToLower(strings.TrimSpace(string(raw)))
		if uuidShape.MatchString(id) {
			return id
		}
		return ""
	}
	_ = os.MkdirAll(filepath.Dir(path), 0o700)
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return ""
	}
	defer f.Close()
	id := session.NewUUID()
	if _, err := f.WriteString(id); err != nil {
		return ""
	}
	return id
}

// OSName is what the Python CLI reports as `os` (platform.system()).
func OSName() string {
	switch runtime.GOOS {
	case "darwin":
		return "Darwin"
	case "linux":
		return "Linux"
	case "windows":
		return "Windows"
	}
	return runtime.GOOS
}

// Command records one finished invocation.
func Command(command string, exitCode int, duration time.Duration, notes map[string]any, channel string) {
	if !Enabled() {
		return
	}
	id := InstallationID()
	if id == "" {
		return
	}
	s := session.Current(config.Path())
	props := map[string]any{}
	for k, v := range notes {
		props[k] = v
	}
	props["command"] = command
	props["exit_code"] = exitCode
	props["success"] = exitCode == 0
	props["duration_ms"] = duration.Milliseconds()
	props["session"] = s.ID
	props["agent"] = s.Agent
	props["cli_version"] = buildinfo.Version
	props["os"] = OSName()
	props["arch"] = runtime.GOARCH
	if channel != "" {
		props["channel"] = channel
	}
	record(Event{Event: "cli_command_completed", DistinctID: id, UUID: session.NewUUID(),
		Timestamp: time.Now().UTC().Format(time.RFC3339Nano), Properties: props})
}

func record(e Event) {
	line, err := json.Marshal(e)
	if err != nil {
		return
	}
	path := config.Sibling(queueName)
	if info, err := os.Stat(path); err == nil && info.Size() > maxQueue {
		return
	}
	_ = os.MkdirAll(filepath.Dir(path), 0o700)
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return
	}
	defer f.Close()
	_, _ = f.Write(append(line, '\n'))
}

// Flush sends the queued events to the registry's relay. It claims the queue by renaming it, so
// two CLIs never send one event twice; whatever it cannot send goes back into the queue, and a
// claim left by a process that died mid-send is picked up once it is a minute old.
func Flush(ctx context.Context, registry string) {
	if !Enabled() {
		return
	}
	ctx, cancel := context.WithTimeout(ctx, sendBudget)
	defer cancel()
	dir := filepath.Dir(config.Path())
	claims := orphans(dir)
	if claim := claimQueue(dir); claim != "" {
		claims = append(claims, claim)
	}
	for _, claim := range claims {
		events := readLines(claim)
		var unsent [][]byte
		for start := 0; start < len(events); start += batchSize {
			end := min(start+batchSize, len(events))
			if !send(ctx, registry, events[start:end]) {
				unsent = append(unsent, events[start:]...)
				break
			}
		}
		if len(unsent) > 0 {
			requeue(dir, unsent)
		}
		_ = os.Remove(claim)
	}
}

func claimQueue(dir string) string {
	queue := filepath.Join(dir, queueName)
	if info, err := os.Stat(queue); err != nil || info.Size() == 0 {
		return ""
	}
	claim := filepath.Join(dir, "telemetry-"+session.NewUUID()+".sending")
	if os.Rename(queue, claim) != nil {
		return ""
	}
	return claim
}

func orphans(dir string) []string {
	matches, _ := filepath.Glob(filepath.Join(dir, "telemetry-*.sending"))
	var old []string
	for _, m := range matches {
		if info, err := os.Stat(m); err == nil && time.Since(info.ModTime()) > orphanAge {
			old = append(old, m)
		}
	}
	return old
}

func readLines(path string) [][]byte {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var lines [][]byte
	sc := bufio.NewScanner(bytes.NewReader(data))
	sc.Buffer(make([]byte, 64*1024), maxQueue)
	for sc.Scan() {
		if line := bytes.TrimSpace(sc.Bytes()); len(line) > 0 && json.Valid(line) {
			lines = append(lines, append([]byte(nil), line...))
		}
	}
	return lines
}

func requeue(dir string, lines [][]byte) {
	f, err := os.OpenFile(filepath.Join(dir, queueName), os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return
	}
	defer f.Close()
	for _, l := range lines {
		_, _ = f.Write(append(l, '\n'))
	}
}

// send reports whether the batch is done with: delivered, or refused for good (a 4xx other than
// 429 would be refused again).
func send(ctx context.Context, registry string, lines [][]byte) bool {
	var body bytes.Buffer
	body.WriteString(`{"events":[`)
	body.Write(bytes.Join(lines, []byte(",")))
	body.WriteString(`]}`)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(registry, "/")+"/cli/events", &body)
	if err != nil {
		return true
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", buildinfo.UserAgent())
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return false
	}
	resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound || resp.StatusCode == http.StatusMethodNotAllowed {
		// A registry without the relay: drop, never retry.
		return true
	}
	return resp.StatusCode < 500 && resp.StatusCode != http.StatusTooManyRequests
}
