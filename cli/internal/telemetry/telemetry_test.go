package telemetry

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"
)

func sandbox(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("TREG_CONFIG", filepath.Join(dir, "config.json"))
	t.Setenv("TREG_TELEMETRY", "")
	t.Setenv("DO_NOT_TRACK", "")
	t.Setenv("TREG_SESSION_ID", "task-1")
	return dir
}

type relay struct {
	srv    *httptest.Server
	events []map[string]any
	status atomic.Int32
}

func newRelay(t *testing.T) *relay {
	r := &relay{}
	r.status.Store(204)
	r.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		body, _ := io.ReadAll(req.Body)
		if req.URL.Path != "/cli/events" {
			t.Errorf("posted to %s", req.URL.Path)
		}
		if code := int(r.status.Load()); code != 204 {
			w.WriteHeader(code)
			return
		}
		var batch struct{ Events []map[string]any }
		_ = json.Unmarshal(body, &batch)
		r.events = append(r.events, batch.Events...)
		w.WriteHeader(204)
	}))
	t.Cleanup(r.srv.Close)
	return r
}

func TestQueuedEventsAreSentByALaterFlush(t *testing.T) {
	dir := sandbox(t)
	r := newRelay(t)
	Command("call", 0, 1500*time.Millisecond, map[string]any{"tool": "a.b"}, "beta")
	Command("whoami", 1, time.Millisecond, nil, "")
	if len(r.events) != 0 {
		t.Fatal("recording sent something")
	}
	Flush(context.Background(), r.srv.URL, http.DefaultTransport)
	if len(r.events) != 2 {
		t.Fatalf("sent %d events", len(r.events))
	}
	first := r.events[0]
	props := first["properties"].(map[string]any)
	if first["event"] != "cli_command_completed" || props["command"] != "call" || props["tool"] != "a.b" ||
		props["exit_code"].(float64) != 0 || props["success"] != true || props["duration_ms"].(float64) != 1500 ||
		props["session"] != "task-1" || props["channel"] != "beta" {
		t.Fatalf("event = %v", first)
	}
	id, _ := os.ReadFile(filepath.Join(dir, "analytics-id"))
	if first["distinct_id"] != string(id) {
		t.Fatal("the event is not keyed by analytics-id")
	}
	Flush(context.Background(), r.srv.URL, http.DefaultTransport)
	if len(r.events) != 2 {
		t.Fatal("a sent event was sent again")
	}
}

func TestUnsentEventsStayQueued(t *testing.T) {
	sandbox(t)
	r := newRelay(t)
	r.status.Store(503)
	Command("call", 0, 0, nil, "")
	Flush(context.Background(), r.srv.URL, http.DefaultTransport)
	r.status.Store(204)
	Flush(context.Background(), r.srv.URL, http.DefaultTransport)
	if len(r.events) != 1 {
		t.Fatalf("sent %d events after the relay recovered", len(r.events))
	}
}

func TestARegistryWithoutTheRelayDropsEvents(t *testing.T) {
	dir := sandbox(t)
	r := newRelay(t)
	r.status.Store(404)
	Command("call", 0, 0, nil, "")
	Flush(context.Background(), r.srv.URL, http.DefaultTransport)
	if _, err := os.Stat(filepath.Join(dir, queueName)); err == nil {
		t.Fatal("events for a registry without the relay were kept")
	}
}

func TestOptOutWritesNothing(t *testing.T) {
	dir := sandbox(t)
	t.Setenv("DO_NOT_TRACK", "1")
	Command("call", 0, 0, nil, "")
	if entries, _ := os.ReadDir(dir); len(entries) != 0 {
		t.Fatalf("opted out but wrote %v", entries)
	}
}

func TestAClaimLeftByADeadProcessIsSentLater(t *testing.T) {
	dir := sandbox(t)
	r := newRelay(t)
	Command("call", 0, 0, nil, "")
	claim := claimQueue(dir)
	old := time.Now().Add(-2 * orphanAge)
	os.Chtimes(claim, old, old)
	Flush(context.Background(), r.srv.URL, http.DefaultTransport)
	if len(r.events) != 1 {
		t.Fatalf("sent %d events", len(r.events))
	}
	if _, err := os.Stat(claim); err == nil {
		t.Fatal("the orphaned claim was left behind")
	}
}
