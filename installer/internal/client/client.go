// Package client is the Go client of the installer API (#139, #141). The TUI (#142) uses it;
// it pins the server certificate by its SHA-256 fingerprint instead of a CA.
package client

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// FieldError is one field of a validation_failed answer.
type FieldError struct {
	Field   string `json:"field"`
	Code    string `json:"code"`
	Message string `json:"message"`
}

// Error is the #139 error body.
type Error struct {
	Status    int          `json:"-"`
	Code      string       `json:"code"`
	Message   string       `json:"message"`
	Fields    []FieldError `json:"fields"`
	Retryable bool         `json:"retryable"`
}

func (e *Error) Error() string { return fmt.Sprintf("%d %s: %s", e.Status, e.Code, e.Message) }

// Client talks to one installer API.
type Client struct {
	Base  string
	HTTP  *http.Client
	token string
}

// PinnedHTTP returns an http.Client that accepts only the certificate with this SHA-256
// fingerprint (hex), the one vws-installer wrote to its state directory.
func PinnedHTTP(fingerprint string) *http.Client {
	want := strings.ToLower(strings.TrimSpace(fingerprint))
	return &http.Client{
		Timeout: 30 * time.Second,
		Transport: &http.Transport{TLSClientConfig: &tls.Config{
			MinVersion:         tls.VersionTLS12,
			InsecureSkipVerify: true, //nolint:gosec // VerifyPeerCertificate pins the exact certificate
			VerifyPeerCertificate: func(raw [][]byte, _ [][]*x509.Certificate) error {
				if len(raw) == 0 {
					return errors.New("no server certificate")
				}
				sum := sha256.Sum256(raw[0])
				if hex.EncodeToString(sum[:]) != want {
					return errors.New("server certificate fingerprint does not match the installer state directory")
				}
				return nil
			},
		}},
	}
}

// Do sends one request. in is JSON-encoded when not nil; out receives a 2xx body.
func (c *Client) Do(ctx context.Context, method, path string, in, out any) error {
	var body io.Reader
	if in != nil {
		b, err := json.Marshal(in)
		if err != nil {
			return fmt.Errorf("encode %s: %w", path, err)
		}
		body = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.Base+"/api/v1"+path, body)
	if err != nil {
		return fmt.Errorf("request %s: %w", path, err)
	}
	req.Header.Set("Content-Type", "application/json")
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return fmt.Errorf("%s %s: %w", method, path, err)
	}
	defer resp.Body.Close() //nolint:errcheck // read-only body
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("%s %s: %w", method, path, err)
	}
	if resp.StatusCode >= 300 {
		var wire struct {
			Error Error `json:"error"`
		}
		if json.Unmarshal(raw, &wire) != nil || wire.Error.Code == "" {
			return &Error{Status: resp.StatusCode, Code: "unknown", Message: strings.TrimSpace(string(raw))}
		}
		wire.Error.Status = resp.StatusCode
		return &wire.Error
	}
	if out != nil {
		if err := json.Unmarshal(raw, out); err != nil {
			return fmt.Errorf("decode %s: %w", path, err)
		}
	}
	return nil
}

// Login exchanges the one-time access code for a session.
func (c *Client) Login(ctx context.Context, code string) error {
	var out struct {
		Token string `json:"token"`
	}
	if err := c.Do(ctx, http.MethodPost, "/session", map[string]string{"code": code}, &out); err != nil {
		return err
	}
	c.token = out.Token
	return nil
}
