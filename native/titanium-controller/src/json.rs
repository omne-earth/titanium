//! A minimal JSON reader and writer, matched to the host's encoder.
//!
//! The host writes frames with Python's `json.dumps(separators=(",", ":"),
//! ensure_ascii=False)`. This module reproduces exactly that spelling on the
//! write side, so a frame encoded here and one encoded there are byte-equal
//! for the same message, and re-encoding a decoded frame is a fixed point.
//!
//! The reader is strict. Anything a well-formed peer would never send --
//! duplicate keys, trailing bytes, a bare fragment, a number with a fraction
//! where a count belongs -- is an error rather than a best guess, because the
//! only reason to see one is that the byte stream is not what it claims.

use std::collections::BTreeMap;
use std::fmt::Write as _;

#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    Str(String),
    Array(Vec<Value>),
    Object(BTreeMap<String, Value>),
}

impl Value {
    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Value::Int(n) => Some(*n),
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Value::Bool(b) => Some(*b),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_object(&self) -> Option<&BTreeMap<String, Value>> {
        match self {
            Value::Object(map) => Some(map),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&[Value]> {
        match self {
            Value::Array(items) => Some(items),
            _ => None,
        }
    }
}

pub fn parse(input: &str) -> Result<Value, String> {
    let mut parser = Parser {
        bytes: input.as_bytes(),
        pos: 0,
    };
    parser.skip_whitespace();
    let value = parser.value()?;
    parser.skip_whitespace();
    if parser.pos != parser.bytes.len() {
        return Err("trailing bytes after the JSON value".into());
    }
    Ok(value)
}

struct Parser<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> Parser<'a> {
    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn skip_whitespace(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.pos += 1;
        }
    }

    fn expect(&mut self, byte: u8) -> Result<(), String> {
        if self.peek() == Some(byte) {
            self.pos += 1;
            Ok(())
        } else {
            Err(format!("expected {:?} at byte {}", byte as char, self.pos))
        }
    }

    fn literal(&mut self, word: &str) -> Result<(), String> {
        if self.bytes[self.pos..].starts_with(word.as_bytes()) {
            self.pos += word.len();
            Ok(())
        } else {
            Err(format!("expected {word:?} at byte {}", self.pos))
        }
    }

    fn value(&mut self) -> Result<Value, String> {
        match self.peek().ok_or("unexpected end of JSON")? {
            b'n' => {
                self.literal("null")?;
                Ok(Value::Null)
            }
            b't' => {
                self.literal("true")?;
                Ok(Value::Bool(true))
            }
            b'f' => {
                self.literal("false")?;
                Ok(Value::Bool(false))
            }
            b'"' => Ok(Value::Str(self.string()?)),
            b'[' => self.array(),
            b'{' => self.object(),
            b'-' | b'0'..=b'9' => self.number(),
            other => Err(format!("unexpected byte {:?}", other as char)),
        }
    }

    fn number(&mut self) -> Result<Value, String> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        while matches!(self.peek(), Some(b'0'..=b'9')) {
            self.pos += 1;
        }
        // Every number this protocol carries is a count, an id, or an exit
        // status. A fraction or an exponent means the peer is describing
        // something else, and rounding it into an integer would invent a value.
        if matches!(self.peek(), Some(b'.' | b'e' | b'E')) {
            return Err("the protocol carries only integers".into());
        }
        let text = std::str::from_utf8(&self.bytes[start..self.pos])
            .map_err(|_| "invalid number".to_string())?;
        text.parse::<i64>()
            .map(Value::Int)
            .map_err(|_| format!("integer out of range: {text}"))
    }

    fn string(&mut self) -> Result<String, String> {
        self.expect(b'"')?;
        let mut out = String::new();
        loop {
            let byte = self.peek().ok_or("unterminated string")?;
            match byte {
                b'"' => {
                    self.pos += 1;
                    return Ok(out);
                }
                b'\\' => {
                    self.pos += 1;
                    let escape = self.peek().ok_or("unterminated escape")?;
                    self.pos += 1;
                    match escape {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{8}'),
                        b'f' => out.push('\u{c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => out.push(self.unicode_escape()?),
                        other => return Err(format!("unknown escape \\{}", other as char)),
                    }
                }
                0x00..=0x1f => return Err("raw control byte inside a string".into()),
                _ => {
                    // Copy one whole UTF-8 sequence: the host writes
                    // non-ASCII raw (ensure_ascii=False).
                    let rest = std::str::from_utf8(&self.bytes[self.pos..])
                        .map_err(|_| "invalid UTF-8 in string".to_string())?;
                    let ch = rest.chars().next().ok_or("unterminated string")?;
                    out.push(ch);
                    self.pos += ch.len_utf8();
                }
            }
        }
    }

    fn unicode_escape(&mut self) -> Result<char, String> {
        let high = self.hex4()?;
        // A surrogate pair is two escapes; a lone surrogate is not a
        // character and is refused rather than replaced.
        if (0xd800..0xdc00).contains(&high) {
            self.expect(b'\\')?;
            self.expect(b'u')?;
            let low = self.hex4()?;
            if !(0xdc00..0xe000).contains(&low) {
                return Err("unpaired high surrogate".into());
            }
            let combined = 0x10000 + ((high - 0xd800) << 10) + (low - 0xdc00);
            return char::from_u32(combined).ok_or_else(|| "invalid surrogate pair".into());
        }
        if (0xdc00..0xe000).contains(&high) {
            return Err("unpaired low surrogate".into());
        }
        char::from_u32(high).ok_or_else(|| "invalid \\u escape".into())
    }

    fn hex4(&mut self) -> Result<u32, String> {
        if self.pos + 4 > self.bytes.len() {
            return Err("truncated \\u escape".into());
        }
        let text = std::str::from_utf8(&self.bytes[self.pos..self.pos + 4])
            .map_err(|_| "invalid \\u escape".to_string())?;
        let value = u32::from_str_radix(text, 16).map_err(|_| "invalid \\u escape".to_string())?;
        self.pos += 4;
        Ok(value)
    }

    fn array(&mut self) -> Result<Value, String> {
        self.expect(b'[')?;
        let mut items = Vec::new();
        self.skip_whitespace();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(Value::Array(items));
        }
        loop {
            self.skip_whitespace();
            items.push(self.value()?);
            self.skip_whitespace();
            match self.peek() {
                Some(b',') => self.pos += 1,
                Some(b']') => {
                    self.pos += 1;
                    return Ok(Value::Array(items));
                }
                _ => return Err("expected ',' or ']' in array".into()),
            }
        }
    }

    fn object(&mut self) -> Result<Value, String> {
        self.expect(b'{')?;
        let mut map = BTreeMap::new();
        self.skip_whitespace();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(Value::Object(map));
        }
        loop {
            self.skip_whitespace();
            let key = self.string()?;
            self.skip_whitespace();
            self.expect(b':')?;
            self.skip_whitespace();
            let value = self.value()?;
            // A duplicate key means two different readers can disagree about
            // the same frame depending on which one they keep.
            if map.insert(key, value).is_some() {
                return Err("duplicate key in object".into());
            }
            self.skip_whitespace();
            match self.peek() {
                Some(b',') => self.pos += 1,
                Some(b'}') => {
                    self.pos += 1;
                    return Ok(Value::Object(map));
                }
                _ => return Err("expected ',' or '}' in object".into()),
            }
        }
    }
}

/// Escape one string exactly as Python's json module does with
/// `ensure_ascii=False`: the seven short escapes, `\u00XX` for the remaining
/// C0 controls, and every other character written raw as UTF-8.
pub fn write_string(out: &mut String, value: &str) {
    out.push('"');
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_a_flat_object() {
        let value = parse(r#"{"v":1,"type":"exec_complete","timed_out":false}"#).unwrap();
        let map = value.as_object().unwrap();
        assert_eq!(map["v"].as_i64(), Some(1));
        assert_eq!(map["type"].as_str(), Some("exec_complete"));
        assert_eq!(map["timed_out"].as_bool(), Some(false));
    }

    #[test]
    fn refuses_trailing_bytes() {
        assert!(parse(r#"{"a":1} junk"#).is_err());
        assert!(parse(r#"{"a":1}{"a":2}"#).is_err());
    }

    #[test]
    fn refuses_duplicate_keys() {
        assert!(parse(r#"{"a":1,"a":2}"#).is_err());
    }

    #[test]
    fn refuses_non_integers() {
        assert!(parse(r#"{"a":1.5}"#).is_err());
        assert!(parse(r#"{"a":1e3}"#).is_err());
    }

    #[test]
    fn refuses_raw_control_bytes_in_strings() {
        assert!(parse("{\"a\":\"x\ny\"}").is_err());
    }

    #[test]
    fn round_trips_escapes_and_non_ascii() {
        let original = "quote\" back\\ tab\t nl\n ctrl\u{1} snow\u{2603}";
        let mut encoded = String::new();
        write_string(&mut encoded, original);
        assert_eq!(parse(&encoded).unwrap().as_str(), Some(original));
        // Matches Python: short escapes, \u00XX for other C0, raw non-ASCII.
        assert!(encoded.contains("\\t"));
        assert!(encoded.contains("\\u0001"));
        assert!(encoded.contains('\u{2603}'));
    }

    #[test]
    fn handles_surrogate_pairs_and_refuses_lone_ones() {
        // Escaped surrogate pair, and the same character written raw.
        assert_eq!(
            parse(r#""\ud83d\ude00""#).unwrap().as_str(),
            Some("\u{1f600}")
        );
        assert_eq!(parse("\"\u{1f600}\"").unwrap().as_str(), Some("\u{1f600}"));
        assert!(parse(r#""\ud83d""#).is_err());
        assert!(parse(r#""\ude00""#).is_err());
    }
}
