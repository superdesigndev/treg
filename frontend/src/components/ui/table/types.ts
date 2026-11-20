// A DataTable column: what it shows, how wide it may get, how it behaves on a phone.
export interface Column<Row = any> {
  key: string
  header: string
  /** One sentence on what the number means, shown on hover or focus of the heading. */
  tip?: string
  align?: 'left' | 'right' | 'center'
  /** A fixed width ("180px", "30%"). Omitted: the column sizes to its content. */
  width?: string
  /** Never narrower than this, even when the table is squeezed. */
  minWidth?: string
  /** One line: longer text ends in an ellipsis, with the whole of it on hover. Needs `width`. */
  truncate?: boolean
  /** Cells stay on one line (shadcn's default); a column of prose or tags opts into wrapping. */
  wrap?: boolean
  sortable?: boolean
  /** The direction a first click sorts in; numbers people want high first say "desc". */
  sortFirst?: 'asc' | 'desc'
  /** The cell's text when no `cell-<key>` slot is given, and the full text behind a truncation. */
  value?: (row: Row) => string | number | null | undefined
  /** Below 760px each row becomes a card: `primary` spans it, `field` is labelled (two to a line),
   *  `wide` is labelled and spans it, `hide` drops out. */
  mobile?: 'primary' | 'field' | 'wide' | 'hide'
}

export interface Sort { key: string, dir: 'asc' | 'desc' }
