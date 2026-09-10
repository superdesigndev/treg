// The maker's data travels WITH the tool: `data.csv` beside these files is uploaded at publish
// (at most 50 MB, read-only; replace it and publish again for a new version). The script reads
// it as ctx.data, already parsed into objects keyed by the header row. No tool, no key, no call.
export default async function run(ctx) {
  const { column, equals, search, limit } = ctx.inputs;
  let rows = ctx.data || [];
  if (column && equals) rows = rows.filter(row => String(row[column] ?? "") === equals);
  if (search) {
    const needle = search.toLowerCase();
    rows = rows.filter(row => Object.values(row).some(v => String(v).toLowerCase().includes(needle)));
  }
  ctx.log(rows.length + " of " + (ctx.data || []).length + " rows matched");
  return { rows: rows.slice(0, limit), count: rows.length };
}
