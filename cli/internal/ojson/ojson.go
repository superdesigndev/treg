// Package ojson is JSON whose objects keep their member order, so a file the CLI edits (an agent's
// MCP config, the skill ledger, a skill's treg.json) keeps the order its owner wrote, and numbers
// keep the spelling they were written in.
package ojson

import (
	"bytes"
	"encoding/json/jsontext"
	"encoding/json/v2"
	"errors"
	"io"
	"strconv"
)

// Object is a JSON object in document order. A key read twice keeps its first position and its
// last value.
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

// Value is the member's value, nil when the key is absent.
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

// MarshalJSONTo writes the members in their order.
func (o *Object) MarshalJSONTo(enc *jsontext.Encoder) error {
	if err := enc.WriteToken(jsontext.BeginObject); err != nil {
		return err
	}
	for _, k := range o.keys {
		if err := enc.WriteToken(jsontext.String(k)); err != nil {
			return err
		}
		if err := json.MarshalEncode(enc, o.values[k]); err != nil {
			return err
		}
	}
	return enc.WriteToken(jsontext.EndObject)
}

// Number is a JSON number as it was written.
type Number string

func (n Number) Int64() (int64, error)     { return strconv.ParseInt(string(n), 10, 64) }
func (n Number) Float64() (float64, error) { return strconv.ParseFloat(string(n), 64) }

func (n Number) MarshalJSONTo(enc *jsontext.Encoder) error {
	return enc.WriteValue(jsontext.Value(n))
}

// Map keys in a stable order, and a string that is not UTF-8 written with replacement characters
// rather than refused: a message piped in from a legacy console is still sent.
var marshal = json.JoinOptions(json.Deterministic(true), jsontext.AllowInvalidUTF8(true))

// Compact is v as JSON on one line with no spaces, as a request body is sent.
func Compact(v any) []byte {
	data, _ := json.Marshal(v, marshal)
	return data
}

// Pretty is v as JSON indented by two spaces, as the CLI writes a file or prints a value.
func Pretty(v any) string {
	data, _ := json.Marshal(v, marshal, jsontext.Multiline(true), jsontext.WithIndent("  "))
	return string(data)
}

// Valid reports whether data is one JSON value, as leniently as Decode reads one.
func Valid(data []byte) bool {
	return jsontext.Value(data).IsValid(jsontext.AllowDuplicateNames(true), jsontext.AllowInvalidUTF8(true))
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
	case Number:
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
	case Number:
		return string(t)
	}
	return string(Compact(v))
}

// List is a decoded array, or nil.
func List(v any) []any {
	items, _ := v.([]any)
	return items
}

// Decode parses JSON keeping object members in document order: objects become *Object, arrays
// []any, numbers Number. A UTF-8 byte-order mark is skipped; a name given twice and a string that
// is not UTF-8 are read, as the files a person edits sometimes hold them.
func Decode(data []byte) (any, error) {
	dec := jsontext.NewDecoder(bytes.NewReader(bytes.TrimPrefix(data, []byte("\xef\xbb\xbf"))),
		jsontext.AllowDuplicateNames(true), jsontext.AllowInvalidUTF8(true))
	v, err := decodeValue(dec)
	if err != nil {
		return nil, err
	}
	if _, err := dec.ReadToken(); err != io.EOF {
		return nil, errors.New("invalid JSON: data after the value")
	}
	return v, nil
}

// Loads is Decode over text.
func Loads(s string) (any, error) { return Decode([]byte(s)) }

func decodeValue(dec *jsontext.Decoder) (any, error) {
	tok, err := dec.ReadToken()
	if err == io.EOF {
		return nil, errors.New("invalid JSON: no value")
	}
	if err != nil {
		return nil, err
	}
	switch tok.Kind() {
	case 'n':
		return nil, nil
	case 't', 'f':
		return tok.Bool(), nil
	case '"':
		return tok.String(), nil
	case '0':
		return Number(tok.String()), nil
	case '{':
		o := NewObject()
		for dec.PeekKind() != '}' {
			key, err := dec.ReadToken()
			if err != nil {
				return nil, err
			}
			name := key.String()
			v, err := decodeValue(dec)
			if err != nil {
				return nil, err
			}
			o.Set(name, v)
		}
		_, err := dec.ReadToken()
		return o, err
	case '[':
		items := []any{}
		for dec.PeekKind() != ']' {
			v, err := decodeValue(dec)
			if err != nil {
				return nil, err
			}
			items = append(items, v)
		}
		_, err := dec.ReadToken()
		return items, err
	}
	return nil, errors.New("invalid JSON: unexpected " + tok.Kind().String())
}
