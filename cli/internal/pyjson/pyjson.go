// Package pyjson reads JSON keeping object member order and writes it byte for byte as Python's
// json.dumps does, so a file the Python CLI and the Go CLI both write (an agent's MCP config, the
// skill ledger, a skill's treg.json) comes out the same whichever wrote it.
package pyjson

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"math/big"
	"sort"
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

// Decode parses one JSON document. Objects become *Object, arrays []any and numbers json.Number.
func Decode(data []byte) (any, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	v, err := decodeValue(dec)
	if err != nil {
		return nil, err
	}
	if _, err := dec.Token(); err != io.EOF {
		return nil, errors.New("extra data after the JSON value")
	}
	return v, nil
}

func decodeValue(dec *json.Decoder) (any, error) {
	tok, err := dec.Token()
	if err != nil {
		return nil, err
	}
	switch t := tok.(type) {
	case json.Delim:
		switch t {
		case '{':
			o := NewObject()
			for dec.More() {
				kt, err := dec.Token()
				if err != nil {
					return nil, err
				}
				key, _ := kt.(string)
				v, err := decodeValue(dec)
				if err != nil {
					return nil, err
				}
				o.Set(key, v)
			}
			if _, err := dec.Token(); err != nil {
				return nil, err
			}
			return o, nil
		case '[':
			arr := []any{}
			for dec.More() {
				v, err := decodeValue(dec)
				if err != nil {
					return nil, err
				}
				arr = append(arr, v)
			}
			if _, err := dec.Token(); err != nil {
				return nil, err
			}
			return arr, nil
		}
		return nil, fmt.Errorf("unexpected %v", t)
	}
	return tok, nil
}

// Options are json.dumps's: Indent < 0 is the compact one-line form with ", " and ": ".
type Options struct {
	Indent      int
	EnsureASCII bool
	SortKeys    bool
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
			b.WriteByte(',')
			if opt.Indent < 0 {
				b.WriteByte(' ')
			}
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
			b.WriteByte(',')
			if opt.Indent < 0 {
				b.WriteByte(' ')
			}
		}
		if opt.Indent >= 0 {
			newline(b, opt, depth+1)
		}
		b.WriteString(Quote(k, opt.EnsureASCII))
		b.WriteString(": ")
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
