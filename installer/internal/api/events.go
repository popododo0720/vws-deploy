package api

import (
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

// EventBuffer is how many events the stream keeps for Last-Event-ID resume (#145).
const EventBuffer = 2000

type event struct {
	ID   uint64
	Type string // log, state, gap
	Data string
}

// eventLog is the ring of masked install events. Ids grow across installs and never repeat.
type eventLog struct {
	mu      sync.Mutex
	ring    []event
	next    uint64 // id of the next event; ids start at 1
	changed chan struct{}
	mask    mask
}

func newEventLog(literals func() []string) *eventLog {
	return &eventLog{ring: make([]event, 0, EventBuffer), next: 1, changed: make(chan struct{}), mask: mask{literals: literals}}
}

// add masks and appends; a log line with newlines becomes one event per line.
func (l *eventLog) add(typ, data string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	for _, line := range strings.Split(strings.ReplaceAll(data, "\r", "\n"), "\n") {
		if typ == "log" {
			line = l.mask.line(line)
		}
		if len(l.ring) == EventBuffer {
			copy(l.ring, l.ring[1:])
			l.ring = l.ring[:EventBuffer-1]
		}
		l.ring = append(l.ring, event{ID: l.next, Type: typ, Data: line})
		l.next++
	}
	close(l.changed)
	l.changed = make(chan struct{})
}

// after returns the events after id, the number of events dropped from the ring between id and
// the oldest kept one, and a channel closed on the next add.
func (l *eventLog) after(id uint64) ([]event, uint64, <-chan struct{}) {
	l.mu.Lock()
	defer l.mu.Unlock()
	var gap uint64
	if len(l.ring) > 0 && id+1 < l.ring[0].ID {
		gap = l.ring[0].ID - id - 1
	}
	var out []event
	for _, e := range l.ring {
		if e.ID > id {
			out = append(out, e)
		}
	}
	return out, gap, l.changed
}

// heartbeat keeps proxies from closing an idle stream.
var heartbeat = 15 * time.Second

func sendEvent(w io.Writer, format string, a ...any) error {
	if _, err := fmt.Fprintf(w, format, a...); err != nil {
		return fmt.Errorf("write event stream: %w", err)
	}
	return nil
}

// getEvents streams the install as server-sent events. Last-Event-ID (header, or the
// lastEventId query for clients that cannot set it) resumes after that id; if the ring no longer
// holds the next one, a gap event says how many were dropped. The stream ends once no install is
// running and every event was sent; EventSource then reconnects after retry and resumes.
func (srv *Server) getEvents(w http.ResponseWriter, r *http.Request) error {
	flusher, ok := w.(http.Flusher)
	if !ok {
		return internal(fmt.Errorf("the response writer cannot stream"))
	}
	last := r.Header.Get("Last-Event-ID")
	if last == "" {
		last = r.URL.Query().Get("lastEventId")
	}
	var id uint64
	if last != "" {
		n, err := strconv.ParseUint(last, 10, 64)
		if err != nil {
			return fail(ReasonBadRequest, "Last-Event-ID must be an event id")
		}
		id = n
	}
	h := w.Header()
	h.Set("Content-Type", "text/event-stream")
	h.Set("Cache-Control", "no-store")
	h.Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	if err := sendEvent(w, "retry: 5000\n\n"); err != nil {
		return internal(err)
	}
	flusher.Flush()
	tick := time.NewTicker(heartbeat)
	defer tick.Stop()
	for {
		evs, gap, changed := srv.events.after(id)
		if gap > 0 {
			if err := sendEvent(w, "event: gap\ndata: %d\n\n", gap); err != nil {
				return internal(err)
			}
		}
		for _, e := range evs {
			if err := sendEvent(w, "id: %d\nevent: %s\ndata: %s\n\n", e.ID, e.Type, e.Data); err != nil {
				return internal(err)
			}
			id = e.ID
		}
		flusher.Flush()
		srv.mu.Lock()
		running := srv.s.state == StateInstalling
		srv.mu.Unlock()
		if !running {
			// One more read: an event added between after and the state check is not lost.
			if evs, _, _ := srv.events.after(id); len(evs) == 0 {
				return nil
			}
			continue
		}
		select {
		case <-r.Context().Done():
			return nil
		case <-changed:
		case <-tick.C:
			if err := sendEvent(w, ": keepalive\n\n"); err != nil {
				return internal(err)
			}
			flusher.Flush()
		}
	}
}
