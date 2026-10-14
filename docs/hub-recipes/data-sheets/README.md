# sheet-rows

Serves the rows of a public Google Sheet as a tool. Give the sheet's id (from its URL) and, if
needed, the tab's `gid`. Filter with `column` + `equals`, or search every cell with `search`;
`limit` up to 100. The sheet must be shared as "anyone with the link"; private sheets are not
supported in this version.

Before publishing: `treg tool add sheets --base-url https://docs.google.com` (no secret), then
list `sheets` in `uses`.
