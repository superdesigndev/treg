package output

import (
	"fmt"
	"io"

	"github.com/superdesigndev/treg/cli/internal/api"
)

// Data is the route's own answer inside a view, as a response Show and refused can read.
func Data(r *api.Response, v *api.View) *api.Response {
	return &api.Response{Status: r.Status, Header: r.Header, Body: v.Data, Cookies: r.Cookies, Path: r.Path}
}

// ShowView prints the text the registry rendered in place of the body. A refusal then gets the
// stderr lines and status Show gives it.
func (p *Printer) ShowView(r *api.Response, v *api.View) error {
	io.WriteString(p.Out, v.Text)
	if r.Status < 400 {
		return nil
	}
	return p.refused(Data(r, v))
}

// ShowRaw prints the route's JSON exactly as the registry sent it, then status 1 for a refusal.
func (p *Printer) ShowRaw(r *api.Response, v *api.View) error {
	p.Out.Write(v.Data)
	fmt.Fprintln(p.Out)
	if r.Status >= 400 {
		return &Exit{Code: 1}
	}
	return nil
}

// ShowUnrendered prints an answer that came without the view this CLI asked for, as from a
// registry older than views: the JSON as Show prints it, and one stderr line saying why.
func (p *Printer) ShowUnrendered(r *api.Response) error {
	err := p.Show(r)
	if !p.JSON {
		fmt.Fprintln(p.Err, "treg: the registry sent its JSON without the text this CLI prints (a registry older than this CLI does); update the registry to get it")
	}
	return err
}
