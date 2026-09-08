//! Standard base64, matching Python's `base64.b64encode` / `b64decode(validate=True)`.
//!
//! Binary program output crosses the wire inside JSON, and JSON cannot carry
//! arbitrary bytes. `validate=True` on the Python side rejects any character
//! outside the alphabet rather than skipping it, so this decoder does the same:
//! silently dropping an unexpected byte would let two peers disagree about what
//! a program printed.

const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

pub fn encode(input: &[u8]) -> String {
    let mut out = String::with_capacity(input.len().div_ceil(3) * 4);
    for chunk in input.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(ALPHABET[(n >> 18) as usize & 63] as char);
        out.push(ALPHABET[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            ALPHABET[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            ALPHABET[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}

fn value_of(byte: u8) -> Option<u32> {
    match byte {
        b'A'..=b'Z' => Some((byte - b'A') as u32),
        b'a'..=b'z' => Some((byte - b'a') as u32 + 26),
        b'0'..=b'9' => Some((byte - b'0') as u32 + 52),
        b'+' => Some(62),
        b'/' => Some(63),
        _ => None,
    }
}

pub fn decode(input: &str) -> Result<Vec<u8>, String> {
    let bytes = input.as_bytes();
    if !bytes.len().is_multiple_of(4) {
        return Err("base64 length is not a multiple of four".into());
    }
    let mut out = Vec::with_capacity(bytes.len() / 4 * 3);
    for block in bytes.chunks(4) {
        let padding = block.iter().rev().take_while(|&&b| b == b'=').count();
        if padding > 2 {
            return Err("base64 block has too much padding".into());
        }
        // Padding is only ever legal in the final block; anywhere else it
        // would silently truncate the payload.
        if padding > 0 && !std::ptr::eq(block.as_ptr(), bytes[bytes.len() - 4..].as_ptr()) {
            return Err("base64 padding appears before the final block".into());
        }
        let mut n: u32 = 0;
        for (index, &byte) in block.iter().enumerate() {
            let value = if byte == b'=' && index >= 4 - padding {
                0
            } else {
                value_of(byte).ok_or("base64 contains a character outside the alphabet")?
            };
            n = (n << 6) | value;
        }
        out.push((n >> 16) as u8);
        if padding < 2 {
            out.push((n >> 8) as u8);
        }
        if padding < 1 {
            out.push(n as u8);
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_every_byte_value() {
        let all: Vec<u8> = (0u8..=255).collect();
        assert_eq!(decode(&encode(&all)).unwrap(), all);
    }

    #[test]
    fn matches_known_vectors() {
        assert_eq!(encode(b""), "");
        assert_eq!(encode(b"f"), "Zg==");
        assert_eq!(encode(b"fo"), "Zm8=");
        assert_eq!(encode(b"foo"), "Zm9v");
        assert_eq!(encode(b"foob"), "Zm9vYg==");
        assert_eq!(encode(&[0x00, 0xff, 0x00]), "AP8A");
    }

    #[test]
    fn refuses_characters_outside_the_alphabet() {
        assert!(decode("Zm9v!!!!").is_err());
        assert!(decode("Zm9 v").is_err());
        assert!(decode("Zm9v\n").is_err());
    }

    #[test]
    fn refuses_bad_length_and_stray_padding() {
        assert!(decode("Zg=").is_err());
        assert!(decode("Zg===").is_err());
        assert!(decode("Zg==Zg==").is_err());
    }
}
