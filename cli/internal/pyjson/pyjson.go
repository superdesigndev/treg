// Package pyjson reads JSON keeping object member order, so a file the CLI edits (an agent's MCP
// config, the skill ledger, a skill's treg.json) keeps the order its owner wrote, and writes it as
// Python's json.dumps does, so the Python CLI and this one write those files the same way while
// both are in use.
package pyjson

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"math"
	"math/big"
	"slices"
	"strconv"
	"strings"
	"unicode/utf16"
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
			slices.Sort(keys)
		}
		encodeObject(b, keys, t.values, opt, depth)
	case map[string]string:
		values := map[string]any{}
		for k, s := range t {
			values[k] = s
		}
		encodeObject(b, slices.Sorted(maps.Keys(values)), values, opt, depth)
	case map[string]any:
		encodeObject(b, slices.Sorted(maps.Keys(t)), t, opt, depth)
	default:
		data, _ := json.Marshal(t)
		b.Write(data)
	}
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

// Truthy says whether a decoded value holds anything: false for null, false, 0, "" and empty
// containers.
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

// Text is a decoded value as one line of text: a string as it is, nothing for null, a number as
// written, anything else as compact JSON.
func Text(v any) string {
	switch t := v.(type) {
	case nil:
		return ""
	case string:
		return t
	case bool:
		return strconv.FormatBool(t)
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

// Decode parses JSON keeping object members in document order: objects become *Object, arrays
// []any, numbers json.Number as written. A UTF-8 byte-order mark is skipped.
func Decode(data []byte) (any, error) {
	dec := json.NewDecoder(bytes.NewReader(bytes.TrimPrefix(data, []byte("\xef\xbb\xbf"))))
	dec.UseNumber()
	v, err := decodeValue(dec)
	if err != nil {
		return nil, err
	}
	if _, err := dec.Token(); err != io.EOF {
		return nil, errors.New("invalid JSON: data after the value")
	}
	return v, nil
}

// Loads is Decode over text.
func Loads(s string) (any, error) { return Decode([]byte(s)) }

func decodeValue(dec *json.Decoder) (any, error) {
	tok, err := dec.Token()
	if err != nil {
		if err == io.EOF {
			return nil, errors.New("invalid JSON: no value")
		}
		return nil, err
	}
	delim, ok := tok.(json.Delim)
	if !ok {
		return tok, nil
	}
	switch delim {
	case '{':
		o := NewObject()
		for dec.More() {
			key, err := dec.Token()
			if err != nil {
				return nil, err
			}
			v, err := decodeValue(dec)
			if err != nil {
				return nil, err
			}
			o.Set(key.(string), v)
		}
		_, err := dec.Token()
		return o, err
	case '[':
		items := []any{}
		for dec.More() {
			v, err := decodeValue(dec)
			if err != nil {
				return nil, err
			}
			items = append(items, v)
		}
		_, err := dec.Token()
		return items, err
	}
	return nil, fmt.Errorf("invalid JSON: unexpected %v", delim)
}
