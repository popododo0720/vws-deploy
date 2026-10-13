package api

import (
	"bufio"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"testing"
	"time"
)

type sse struct{ id, typ, data string }

// stream reads the event stream to its end (the install ended and every event was sent).
func (h *harness) stream(lastID string) (int, []sse, string) {
	h.t.Helper()
	req, _ := http.NewRequest("GET", h.http.URL+"/api/v1/install/events", nil)
	req.Header.Set("Authorization", "Bearer "+h.token)
	if lastID != "" {
		req.Header.Set("Last-Event-ID", lastID)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		h.t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode == 200 && resp.Header.Get("Content-Type") != "text/event-stream" {
		h.t.Fatalf("content type %q", resp.Header.Get("Content-Type"))
	}
	var evs []sse
	var cur sse
	sc := bufio.NewScanner(strings.NewReader(string(raw)))
	for sc.Scan() {
		k, v, _ := strings.Cut(sc.Text(), ": ")
		switch k {
		case "id":
			cur.id = v
		case "event":
			cur.typ = v
		case "data":
			cur.data = v
		case "":
			if cur.typ != "" {
				evs = append(evs, cur)
			}
			cur = sse{}
		}
	}
	return resp.StatusCode, evs, string(raw)
}

func (h *harness) install() {
	h.t.Helper()
	hash := h.summary()
	if status, body := h.do("POST", "/api/v1/install", map[string]any{"confirm": true, "specHash": hash}); status != 202 {
		h.t.Fatalf("install %d %v", status, body)
	}
}

const fakeSecret = "Fake-Secret-9vQ3xLm"

func TestEventsStreamTheInstallMasked(t *testing.T) {
	h := newHarness(t, nil)
	h.login()
	h.fill()
	h.cluster.lines = []string{
		"helm upgrade vws",
		"admin password=" + fakeSecret,
		`secret {"adminPassword":"` + fakeSecret + `"}`,
		"pull https://robot:" + fakeSecret + "@registry.example.invalid/vws",
		"Authorization: Bearer " + fakeSecret,
		"-----BEGIN PRIVATE KEY-----",
		fakeSecret,
		"-----END PRIVATE KEY-----",
		"login with " + h.code() + " and " + h.token,
		"two\nlines",
	}
	h.install()
	// A client attached while the install runs gets the events as they come and the end.
	got := make(chan []sse)
	go func() { _, evs, _ := h.stream(""); got <- evs }()
	time.Sleep(50 * time.Millisecond)
	h.cluster.release <- nil
	var live []sse
	select {
	case live = <-got:
	case <-time.After(10 * time.Second):
		t.Fatal("the stream did not end after the install")
	}
	h.waitState("succeeded")
	status, evs, raw := h.stream("")
	if status != 200 {
		t.Fatalf("status %d", status)
	}
	if fmt.Sprint(live) != fmt.Sprint(evs) {
		t.Fatalf("live %v\nreplay %v", live, evs)
	}
	for _, s := range []string{fakeSecret, h.token, h.code()} {
		if strings.Contains(raw, s) {
			t.Fatalf("a secret reached the stream:\n%s", raw)
		}
	}
	if evs[0].typ != "state" || evs[0].data != "installing" || evs[len(evs)-1].data != "succeeded" {
		t.Fatalf("events %v", evs)
	}
	var data []string
	for i, e := range evs {
		if e.id != strconv.Itoa(i+1) {
			t.Fatalf("event %d id %s", i, e.id)
		}
		data = append(data, e.data)
	}
	all := strings.Join(data, "|")
	for _, want := range []string{"helm upgrade vws", "admin password=***", "Bearer ***", "two|lines", "robot:***@"} {
		if !strings.Contains(all, want) {
			t.Fatalf("missing %q in %s", want, all)
		}
	}
	// Resume after id 3.
	_, evs, _ = h.stream("3")
	if evs[0].id != "4" || len(evs) != len(data)-3 {
		t.Fatalf("resume %v", evs)
	}
	if status, _, _ := h.stream("x"); status != 400 {
		t.Fatalf("bad Last-Event-ID: %d", status)
	}
}

func TestEventsRingDropsTheOldest(t *testing.T) {
	h := newHarness(t, nil)
	h.login()
	h.fill()
	for i := 0; i < EventBuffer+10; i++ {
		h.cluster.lines = append(h.cluster.lines, "line "+strconv.Itoa(i))
	}
	h.install()
	h.cluster.release <- nil
	h.waitState("succeeded")
	_, evs, _ := h.stream("")
	if evs[0].typ != "gap" || len(evs) != EventBuffer+1 {
		t.Fatalf("first %v, %d events", evs[0], len(evs))
	}
	// 1 state + 1 "installing" + 2010 lines + 1 state = 2013 events; ids 14..2013 are kept.
	if evs[0].data != "13" || evs[1].id != "14" {
		t.Fatalf("gap %v then %v", evs[0], evs[1])
	}
	_, evs, _ = h.stream("2000")
	if evs[0].typ == "gap" || evs[0].id != "2001" {
		t.Fatalf("resume inside the ring %v", evs[0])
	}
}

func TestEventsBeforeAnInstall(t *testing.T) {
	h := newHarness(t, nil)
	h.login()
	status, evs, raw := h.stream("")
	if status != 200 || len(evs) != 0 || !strings.Contains(raw, "retry: 5000") {
		t.Fatalf("%d %v %q", status, evs, raw)
	}
}
