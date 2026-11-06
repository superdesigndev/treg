// Package pyjson reads JSON keeping object member order and writes it byte for byte as Python's
// json.dumps does, so a file the Python CLI and the Go CLI both write (an agent's MCP config, the
// skill ledger, a skill's treg.json) comes out the same whichever wrote it, and a request body is
// the bytes httpx sent. A document that does not parse is refused in json.loads's own words.
package pyjson

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"math/big"
	"sort"
	"strconv"
	"strings"
	"unicode/utf16"
	"unicode/utf8"
)

// Object is a JSON object in document order. A key read twice keeps its first position and its
// last value, as a Python dict built by json.loads does.
type Object struct {
	keys   []string
	values map[string]any
}

func NewObject() *Object { return &Object{values: map[string]any{}} }

// Ordered builds an object from key, value pairs.
func Ordered(kv ...any) *Object {
	o := NewObject()
	for i := 0; i+1 < len(kv); i += 2 {
		o.Set(kv[i].(string), kv[i+1])
	}
	return o
}

func (o *Object) Get(key string) (any, bool) {
	if o == nil {
		return nil, false
	}
	v, ok := o.values[key]
	return v, ok
}

func (o *Object) Set(key string, value any) {
	if _, ok := o.values[key]; !ok {
		o.keys = append(o.keys, key)
	}
	o.values[key] = value
}

func (o *Object) Delete(key string) {
	if _, ok := o.values[key]; !ok {
		return
	}
	delete(o.values, key)
	for i, k := range o.keys {
		if k == key {
			o.keys = append(o.keys[:i:i], o.keys[i+1:]...)
			return
		}
	}
}

func (o *Object) Keys() []string {
	if o == nil {
		return nil
	}
	return append([]string(nil), o.keys...)
}

func (o *Object) Len() int {
	if o == nil {
		return 0
	}
	return len(o.keys)
}

// Copy is a shallow copy: the same values under a new key order.
func (o *Object) Copy() *Object {
	c := NewObject()
	if o == nil {
		return c
	}
	for _, k := range o.keys {
		c.Set(k, o.values[k])
	}
	return c
}

// Value is Python's dict.get(key): nil when the key is absent.
func (o *Object) Value(key string) any {
	v, _ := o.Get(key)
	return v
}

// String is the member's value when it is a JSON string, else "".
func (o *Object) String(key string) string {
	v, _ := o.Get(key)
	s, _ := v.(string)
	return s
}

// MarshalJSON lets an Object travel in an ordinary request body.
func (o *Object) MarshalJSON() ([]byte, error) {
	return []byte(Dumps(o, Options{Indent: -1})), nil
}

// Options are json.dumps's: Indent < 0 is the one-line form. ItemSep and KeySep are
// separators=(ItemSep, KeySep); left empty they are Python's defaults, ", " (or "," with an
// indent) and ": ".
type Options struct {
	Indent          int
	EnsureASCII     bool
	SortKeys        bool
	ItemSep, KeySep string
}

var (
	// Body is what httpx sends for `json=`: one line, "," and ":", UTF-8 kept.
	Body = Options{Indent: -1, ItemSep: ",", KeySep: ":"}
	// Pretty is json.dumps(value, indent=2).
	Pretty = Options{Indent: 2, EnsureASCII: true}
	// Line is json.dumps(value).
	Line = Options{Indent: -1, EnsureASCII: true}
)

func (o Options) itemSep() string {
	switch {
	case o.ItemSep != "":
		return o.ItemSep
	case o.Indent < 0:
		return ", "
	}
	return ","
}

func (o Options) keySep() string {
	if o.KeySep != "" {
		return o.KeySep
	}
	return ": "
}

// Dumps is json.dumps(v, indent=..., ensure_ascii=..., sort_keys=...).
func Dumps(v any, opt Options) string {
	var b strings.Builder
	encode(&b, v, opt, 0)
	return b.String()
}

func encode(b *strings.Builder, v any, opt Options, depth int) {
	switch t := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		if t {
			b.WriteString("true")
		} else {
			b.WriteString("false")
		}
	case string:
		b.WriteString(Quote(t, opt.EnsureASCII))
	case json.Number:
		b.WriteString(number(string(t)))
	case int:
		b.WriteString(strconv.Itoa(t))
	case int64:
		b.WriteString(strconv.FormatInt(t, 10))
	case float64:
		b.WriteString(FloatRepr(t))
	case []string:
		items := make([]any, len(t))
		for i, s := range t {
			items[i] = s
		}
		encodeArray(b, items, opt, depth)
	case []any:
		encodeArray(b, t, opt, depth)
	case *Object:
		keys := t.keys
		if opt.SortKeys {
			keys = append([]string(nil), keys...)
			sort.Strings(keys)
		}
		encodeObject(b, keys, t.values, opt, depth)
	case map[string]string:
		values := map[string]any{}
		for k, s := range t {
			values[k] = s
		}
		encodeObject(b, sortedKeys(values), values, opt, depth)
	case map[string]any:
		encodeObject(b, sortedKeys(t), t, opt, depth)
	default:
		data, _ := json.Marshal(t)
		b.Write(data)
	}
}

func sortedKeys(m map[string]any) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

func newline(b *strings.Builder, opt Options, depth int) {
	b.WriteByte('\n')
	b.WriteString(strings.Repeat(" ", opt.Indent*depth))
}

func encodeArray(b *strings.Builder, items []any, opt Options, depth int) {
	if len(items) == 0 {
		b.WriteString("[]")
		return
	}
	b.WriteByte('[')
	for i, item := range items {
		if i > 0 {
			b.WriteString(opt.itemSep())
		}
		if opt.Indent >= 0 {
			newline(b, opt, depth+1)
		}
		encode(b, item, opt, depth+1)
	}
	if opt.Indent >= 0 {
		newline(b, opt, depth)
	}
	b.WriteByte(']')
}

func encodeObject(b *strings.Builder, keys []string, values map[string]any, opt Options, depth int) {
	if len(keys) == 0 {
		b.WriteString("{}")
		return
	}
	b.WriteByte('{')
	for i, k := range keys {
		if i > 0 {
			b.WriteString(opt.itemSep())
		}
		if opt.Indent >= 0 {
			newline(b, opt, depth+1)
		}
		b.WriteString(Quote(k, opt.EnsureASCII))
		b.WriteString(opt.keySep())
		encode(b, values[k], opt, depth+1)
	}
	if opt.Indent >= 0 {
		newline(b, opt, depth)
	}
	b.WriteByte('}')
}

// Quote is a JSON string literal as Python writes it.
func Quote(s string, ensureASCII bool) string {
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		default:
			switch {
			case r < 0x20:
				fmt.Fprintf(&b, `\u%04x`, r)
			case ensureASCII && r > 0x7e:
				if r > 0xffff {
					hi, lo := utf16.EncodeRune(r)
					fmt.Fprintf(&b, `\u%04x\u%04x`, hi, lo)
				} else {
					fmt.Fprintf(&b, `\u%04x`, r)
				}
			default:
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
	return b.String()
}

// number re-spells a JSON number as Python's int or float does after json.loads.
func number(s string) string {
	if !strings.ContainsAny(s, ".eE") {
		n, ok := new(big.Int).SetString(s, 10)
		if !ok {
			return s
		}
		return n.String()
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil && !math.IsInf(f, 0) {
		return s
	}
	return FloatRepr(f)
}

// FloatRepr is Python's repr() of a float, which json.dumps writes.
func FloatRepr(f float64) string {
	switch {
	case math.IsNaN(f):
		return "NaN"
	case math.IsInf(f, 1):
		return "Infinity"
	case math.IsInf(f, -1):
		return "-Infinity"
	}
	sci := strconv.FormatFloat(f, 'e', -1, 64)
	sign := ""
	if strings.HasPrefix(sci, "-") {
		sign, sci = "-", sci[1:]
	}
	mant, expPart, _ := strings.Cut(sci, "e")
	exp, _ := strconv.Atoi(expPart)
	digits := strings.Replace(mant, ".", "", 1)
	decpt := exp + 1
	if decpt <= -4 || decpt > 16 {
		out := digits[:1]
		if len(digits) > 1 {
			out += "." + digits[1:]
		}
		e := decpt - 1
		esign := "+"
		if e < 0 {
			esign, e = "-", -e
		}
		return fmt.Sprintf("%s%se%s%02d", sign, out, esign, e)
	}
	switch {
	case decpt <= 0:
		return sign + "0." + strings.Repeat("0", -decpt) + digits
	case decpt >= len(digits):
		return sign + digits + strings.Repeat("0", decpt-len(digits)) + ".0"
	}
	return sign + digits[:decpt] + "." + digits[decpt:]
}

// Equal compares two decoded values the way Python compares the dicts json.loads returns.
func Equal(a, b any) bool {
	switch x := a.(type) {
	case *Object:
		y, ok := b.(*Object)
		if !ok || x.Len() != y.Len() {
			return false
		}
		for _, k := range x.keys {
			yv, ok := y.values[k]
			if !ok || !Equal(x.values[k], yv) {
				return false
			}
		}
		return true
	case []any:
		y, ok := b.([]any)
		if !ok || len(x) != len(y) {
			return false
		}
		for i := range x {
			if !Equal(x[i], y[i]) {
				return false
			}
		}
		return true
	case json.Number:
		y, ok := b.(json.Number)
		return ok && number(string(x)) == number(string(y))
	}
	return a == b
}

// Truthy is Python's bool() of a decoded value.
func Truthy(v any) bool {
	switch t := v.(type) {
	case nil:
		return false
	case bool:
		return t
	case string:
		return t != ""
	case json.Number:
		f, _ := t.Float64()
		return f != 0
	case []any:
		return len(t) > 0
	case *Object:
		return t.Len() > 0
	}
	return true
}

// Text is Python's str() of a decoded value.
func Text(v any) string {
	switch t := v.(type) {
	case nil:
		return "None"
	case string:
		return t
	case bool:
		if t {
			return "True"
		}
		return "False"
	case json.Number:
		return number(string(t))
	}
	return Dumps(v, Options{Indent: -1})
}

// List is a decoded array, or nil.
func List(v any) []any {
	items, _ := v.([]any)
	return items
}

// Repr is Python's repr() of a decoded JSON value.
func Repr(v any) string {
	switch t := v.(type) {
	case nil:
		return "None"
	case bool:
		if t {
			return "True"
		}
		return "False"
	case string:
		return reprString(t)
	case json.Number:
		if !strings.ContainsAny(string(t), ".eE") {
			return number(string(t))
		}
		f, _ := strconv.ParseFloat(string(t), 64)
		return reprFloat(f)
	case float64:
		return reprFloat(t)
	case int:
		return strconv.Itoa(t)
	case int64:
		return strconv.FormatInt(t, 10)
	case []any:
		parts := make([]string, len(t))
		for i, x := range t {
			parts[i] = Repr(x)
		}
		return "[" + strings.Join(parts, ", ") + "]"
	case []string:
		parts := make([]string, len(t))
		for i, x := range t {
			parts[i] = reprString(x)
		}
		return "[" + strings.Join(parts, ", ") + "]"
	case *Object:
		parts := make([]string, 0, t.Len())
		for _, k := range t.Keys() {
			parts = append(parts, reprString(k)+": "+Repr(t.values[k]))
		}
		return "{" + strings.Join(parts, ", ") + "}"
	}
	return fmt.Sprint(v)
}

// reprString is Python's repr() of a str: single quotes unless the text has a single quote and no
// double quote, with control characters escaped.
func reprString(s string) string {
	quote := byte('\'')
	if strings.Contains(s, "'") && !strings.Contains(s, `"`) {
		quote = '"'
	}
	var b strings.Builder
	b.WriteByte(quote)
	for _, r := range s {
		switch {
		case r == '\\':
			b.WriteString(`\\`)
		case r == rune(quote):
			b.WriteByte('\\')
			b.WriteRune(r)
		case r == '\n':
			b.WriteString(`\n`)
		case r == '\r':
			b.WriteString(`\r`)
		case r == '\t':
			b.WriteString(`\t`)
		case r < 0x20 || r == 0x7f:
			fmt.Fprintf(&b, `\x%02x`, r)
		default:
			b.WriteRune(r)
		}
	}
	b.WriteByte(quote)
	return b.String()
}

// reprFloat is repr() of a float, which spells the specials "nan" and "inf" where JSON spells
// them NaN and Infinity.
func reprFloat(f float64) string {
	switch {
	case math.IsNaN(f):
		return "nan"
	case math.IsInf(f, 1):
		return "inf"
	case math.IsInf(f, -1):
		return "-inf"
	}
	return FloatRepr(f)
}

// DecodeError is json.JSONDecodeError; Error() is its str().
type DecodeError struct {
	Msg               string
	Pos, Line, Column int
}

func (e *DecodeError) Error() string {
	return fmt.Sprintf("%s: line %d column %d (char %d)", e.Msg, e.Line, e.Column, e.Pos)
}

// Decode is json.loads over bytes, as httpx's Response.json() and a file read call it: a UTF-8
// byte-order mark is skipped. Objects become *Object, arrays []any, numbers json.Number as written,
// and NaN and Infinity float64.
func Decode(data []byte) (any, error) {
	return Loads(string(bytes.TrimPrefix(data, []byte("\xef\xbb\xbf"))))
}

// Loads is json.loads(str): a byte-order mark here is an error, as it is to Python.
func Loads(s string) (any, error) {
	d := &decoder{s: []rune(strings.ToValidUTF8(s, "\uFFFD"))}
	if len(d.s) > 0 && d.s[0] == '\ufeff' {
		return nil, d.err("Unexpected UTF-8 BOM (decode using utf-8-sig)", 0)
	}
	idx := d.skip(0)
	v, end, err := d.scan(idx)
	if err != nil {
		return nil, err
	}
	end = d.skip(end)
	if end != len(d.s) {
		return nil, d.err("Extra data", end)
	}
	return v, nil
}

type decoder struct {
	s []rune
}

// stopIteration is the scanner's "no value here", which json.loads reports as "Expecting value".
type stopIteration struct{ pos int }

func (s *stopIteration) Error() string { return "stop" }

func (d *decoder) err(msg string, pos int) *DecodeError {
	line := 1
	last := -1
	for i := 0; i < pos && i < len(d.s); i++ {
		if d.s[i] == '\n' {
			line++
			last = i
		}
	}
	return &DecodeError{Msg: msg, Pos: pos, Line: line, Column: pos - last}
}

func (d *decoder) skip(i int) int {
	for i < len(d.s) && isSpace(d.s[i]) {
		i++
	}
	return i
}

func isSpace(r rune) bool { return r == ' ' || r == '\t' || r == '\n' || r == '\r' }

func (d *decoder) has(i int, word string) bool {
	w := []rune(word)
	if i+len(w) > len(d.s) {
		return false
	}
	for k, r := range w {
		if d.s[i+k] != r {
			return false
		}
	}
	return true
}

func (d *decoder) scan(idx int) (any, int, error) {
	v, end, err := d.scanOnce(idx)
	if stop, ok := err.(*stopIteration); ok {
		return nil, 0, d.err("Expecting value", stop.pos)
	}
	return v, end, err
}

func (d *decoder) scanOnce(idx int) (any, int, error) {
	if idx >= len(d.s) {
		return nil, 0, &stopIteration{idx}
	}
	switch d.s[idx] {
	case '"':
		return d.scanString(idx + 1)
	case '{':
		return d.scanObject(idx + 1)
	case '[':
		return d.scanArray(idx + 1)
	case 'n':
		if d.has(idx, "null") {
			return nil, idx + 4, nil
		}
	case 't':
		if d.has(idx, "true") {
			return true, idx + 4, nil
		}
	case 'f':
		if d.has(idx, "false") {
			return false, idx + 5, nil
		}
	case 'N':
		if d.has(idx, "NaN") {
			return math.NaN(), idx + 3, nil
		}
	case 'I':
		if d.has(idx, "Infinity") {
			return math.Inf(1), idx + 8, nil
		}
	case '-':
		if d.has(idx, "-Infinity") {
			return math.Inf(-1), idx + 9, nil
		}
	}
	return d.scanNumber(idx)
}

func isDigit(r rune) bool { return r >= '0' && r <= '9' }

func (d *decoder) scanNumber(start int) (any, int, error) {
	s, idx, end := d.s, start, len(d.s)-1
	if s[idx] == '-' {
		idx++
		if idx > end {
			return nil, 0, &stopIteration{start}
		}
	}
	switch {
	case s[idx] >= '1' && s[idx] <= '9':
		idx++
		for idx <= end && isDigit(s[idx]) {
			idx++
		}
	case s[idx] == '0':
		idx++
	default:
		return nil, 0, &stopIteration{start}
	}
	if idx < end && s[idx] == '.' && isDigit(s[idx+1]) {
		idx += 2
		for idx <= end && isDigit(s[idx]) {
			idx++
		}
	}
	if idx < end && (s[idx] == 'e' || s[idx] == 'E') {
		eStart := idx
		idx++
		if idx < end && (s[idx] == '-' || s[idx] == '+') {
			idx++
		}
		for idx <= end && isDigit(s[idx]) {
			idx++
		}
		if !isDigit(s[idx-1]) {
			idx = eStart
		}
	}
	return json.Number(string(s[start:idx])), idx, nil
}

func (d *decoder) scanString(end int) (any, int, error) {
	s := d.s
	begin := end - 1
	var b strings.Builder
	for {
		next := end
		var c rune
		for ; next < len(s); next++ {
			c = s[next]
			if c == '"' || c == '\\' {
				break
			}
			if c <= 0x1f {
				return nil, 0, d.err("Invalid control character at", next)
			}
		}
		if next >= len(s) || (c != '"' && c != '\\') {
			return nil, 0, d.err("Unterminated string starting at", begin)
		}
		b.WriteString(string(s[end:next]))
		next++
		if c == '"' {
			return b.String(), next, nil
		}
		if next == len(s) {
			return nil, 0, d.err("Unterminated string starting at", begin)
		}
		c = s[next]
		if c != 'u' {
			end = next + 1
			switch c {
			case '"', '\\', '/':
			case 'b':
				c = '\b'
			case 'f':
				c = '\f'
			case 'n':
				c = '\n'
			case 'r':
				c = '\r'
			case 't':
				c = '\t'
			default:
				return nil, 0, d.err("Invalid \\escape", end-2)
			}
			b.WriteRune(c)
			continue
		}
		next++
		end = next + 4
		if end >= len(s) {
			return nil, 0, d.err("Invalid \\uXXXX escape", next-1)
		}
		u, ok := hex4(s[next:end])
		if !ok {
			return nil, 0, d.err("Invalid \\uXXXX escape", end-5)
		}
		next = end
		if u >= 0xd800 && u <= 0xdbff && end+6 < len(s) && s[next] == '\\' && s[next+1] == 'u' {
			end += 6
			low, ok := hex4(s[next+2 : end])
			if !ok {
				return nil, 0, d.err("Invalid \\uXXXX escape", end-5)
			}
			if low >= 0xdc00 && low <= 0xdfff {
				u = 0x10000 + (u-0xd800)<<10 + (low - 0xdc00)
			} else {
				end -= 6
			}
		}
		if !utf8.ValidRune(u) {
			u = utf8.RuneError
		}
		b.WriteRune(u)
	}
}

func hex4(rs []rune) (rune, bool) {
	var v rune
	for _, r := range rs {
		v <<= 4
		switch {
		case r >= '0' && r <= '9':
			v |= r - '0'
		case r >= 'a' && r <= 'f':
			v |= r - 'a' + 10
		case r >= 'A' && r <= 'F':
			v |= r - 'A' + 10
		default:
			return 0, false
		}
	}
	return v, true
}

func (d *decoder) scanObject(idx int) (any, int, error) {
	s, end := d.s, len(d.s)-1
	o := NewObject()
	idx = d.skip(idx)
	if idx > end || s[idx] != '}' {
		for {
			if idx > end || s[idx] != '"' {
				return nil, 0, d.err("Expecting property name enclosed in double quotes", idx)
			}
			key, next, err := d.scanString(idx + 1)
			if err != nil {
				return nil, 0, err
			}
			idx = d.skip(next)
			if idx > end || s[idx] != ':' {
				return nil, 0, d.err("Expecting ':' delimiter", idx)
			}
			idx = d.skip(idx + 1)
			val, next, err := d.scanOnce(idx)
			if err != nil {
				return nil, 0, err
			}
			o.Set(key.(string), val)
			idx = d.skip(next)
			if idx <= end && s[idx] == '}' {
				break
			}
			if idx > end || s[idx] != ',' {
				return nil, 0, d.err("Expecting ',' delimiter", idx)
			}
			comma := idx
			idx = d.skip(idx + 1)
			if idx <= end && s[idx] == '}' {
				return nil, 0, d.err("Illegal trailing comma before end of object", comma)
			}
		}
	}
	return o, idx + 1, nil
}

func (d *decoder) scanArray(idx int) (any, int, error) {
	s, end := d.s, len(d.s)-1
	items := []any{}
	idx = d.skip(idx)
	if idx > end || s[idx] != ']' {
		for {
			val, next, err := d.scanOnce(idx)
			if err != nil {
				return nil, 0, err
			}
			items = append(items, val)
			idx = d.skip(next)
			if idx <= end && s[idx] == ']' {
				break
			}
			if idx > end || s[idx] != ',' {
				return nil, 0, d.err("Expecting ',' delimiter", idx)
			}
			comma := idx
			idx = d.skip(idx + 1)
			if idx <= end && s[idx] == ']' {
				return nil, 0, d.err("Illegal trailing comma before end of array", comma)
			}
		}
	}
	return items, idx + 1, nil
}
