package api

import (
	"encoding/json"
	"time"
)

// ViewHeader asks a route for its server-rendered view. A registry that renders one answers
// `{"data": <the route's own JSON body>, "text": <what to print>}` and sets the same header.
const ViewHeader = "X-Treg-View"

// WithView is the request header that asks for a view.
var WithView = [2]string{ViewHeader, "1"}

type View struct {
	Data json.RawMessage `json:"data"`
	Text string          `json:"text"`
}

// View is the response's view, or false when the registry answered without one.
func (r *Response) View() (*View, bool) {
	if r.Header.Get(ViewHeader) != "1" {
		return nil, false
	}
	var v View
	if json.Unmarshal(r.Body, &v) != nil || v.Data == nil {
		return nil, false
	}
	return &v, true
}

// WithTimeout bounds every request this client makes, connection and body included.
func (c *Client) WithTimeout(d time.Duration) *Client {
	c.http.Timeout = d
	return c
}
