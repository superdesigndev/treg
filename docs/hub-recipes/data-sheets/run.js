// A PUBLIC Google Sheet (File → Share → Anyone with the link) exports as CSV at
//   https://docs.google.com/spreadsheets/d/<sheet_id>/export?format=csv&gid=<gid>
// The team registers that host once as its own tool, with no secret:
//   treg tool add sheets --base-url https://docs.google.com
// and lists "sheets" in `uses`. The script names the tool; treg makes the request.
export default async function run(ctx) {
  const { sheet_id, gid, column, equals, search, limit } = ctx.inputs;
  const r = await ctx.call("sheets/spreadsheets/d/" + sheet_id + "/export", {
    query: { format: "csv", gid: String(gid) },
  });
  if (r.status !== 200) throw new Error("the sheet answered " + r.status + " (is it shared as 'anyone with the link'?)");
  let rows = ctx.csv(r.text);                       // objects keyed by the header row
  if (column && equals) rows = rows.filter(row => String(row[column] ?? "") === equals);
  if (search) {
    const needle = search.toLowerCase();
    rows = rows.filter(row => Object.values(row).some(v => String(v).toLowerCase().includes(needle)));
  }
  ctx.log(rows.length + " rows matched; returning " + Math.min(rows.length, limit));
  return { rows: rows.slice(0, limit), count: rows.length };
}
