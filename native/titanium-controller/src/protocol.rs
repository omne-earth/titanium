//! The control protocol, as the guest speaks it.
//!
//! One framing, one JSON spelling, shared with the host's Python
//! implementation in `titanium/environments/cella/wire.py`. A frame is a
//! four-byte big-endian length followed by that many bytes of compact UTF-8
//! JSON. Binary program output travels base64-encoded inside it, because JSON
//! cannot carry arbitrary bytes.
//!
//! Decoding is strict about the field set: a message with an unexpected,
//! missing or duplicated key is refused rather than read for the parts that
//! happen to be recognisable. Two peers that disagree about a frame's shape
//! do not have a protocol, and the safe move on the guest side is to stop
//! talking rather than to act on a guess.
//!
//! Note on byte-exactness: a guest-to-host message re-encoded here is
//! byte-equal to the host's encoding of the same message. An `ExecRequest`
//! decoded and re-encoded need not be, because its `env` is a map and this
//! decoder sorts keys while the host preserves insertion order. The controller
//! only ever decodes that message, never re-emits it, so the difference is not
//! observable on the wire.

use std::collections::BTreeMap;

use crate::base64;
use crate::json::{self, Value};

pub const PROTOCOL_VERSION: i64 = 1;
pub const HEADER_SIZE: usize = 4;
pub const MAX_FRAME_BYTES: usize = 16 * 1024 * 1024;
pub const MAX_OUTPUT_CHUNK_BYTES: usize = 64 * 1024;
pub const MAX_ERROR_MESSAGE_BYTES: usize = 4096;

/// The reasons a valid, understood request could not be carried out. Kept in
/// lockstep with `control.CONTROL_ERROR_CODES` on the host; an unknown code is
/// refused on both sides so that a caller's match on these is exhaustive.
pub const CONTROL_ERROR_CODES: [&str; 5] = [
    "invalid_identity",
    "cwd_unavailable",
    "spawn_failed",
    "finalized",
    "finalization_failed",
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProtocolError(pub String);

impl std::fmt::Display for ProtocolError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl std::error::Error for ProtocolError {}

fn err<T>(message: impl Into<String>) -> Result<T, ProtocolError> {
    Err(ProtocolError(message.into()))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Stream {
    Stdout,
    Stderr,
}

impl Stream {
    fn as_str(&self) -> &'static str {
        match self {
            Stream::Stdout => "stdout",
            Stream::Stderr => "stderr",
        }
    }
}

/// Everything the host may send to the guest.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HostMessage {
    Exec(ExecRequest),
    Finalize(FinalizeRequest),
}

/// Everything the guest may send to the host.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GuestMessage {
    ControllerReady(ControllerReady),
    ExecOutputChunk(ExecOutputChunk),
    ExecComplete(ExecComplete),
    ControlError(ControlError),
    FinalizeComplete(FinalizeComplete),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExecRequest {
    pub request_id: i64,
    pub argv: Vec<String>,
    pub cwd: Option<String>,
    pub env: BTreeMap<String, String>,
    pub timeout_sec: Option<i64>,
    /// The identity the host asked for, unresolved. Carrying it verbatim is
    /// deliberate: the wire check only confirms its shape, and the identity
    /// policy that decides whether it may run is applied separately. Treating
    /// a well-formed field as an authorization would make the wire the
    /// authority on who runs.
    pub user: UserField,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UserField {
    Name(String),
    Uid(i64),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FinalizeRequest {
    pub request_id: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ControllerReady {
    pub protocol_version: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExecOutputChunk {
    pub request_id: i64,
    pub stream: Stream,
    pub data: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExecComplete {
    pub request_id: i64,
    pub return_code: i64,
    /// A started program that hit its deadline. Not an error: the request was
    /// understood and ran, so the outcome belongs on the completion frame
    /// rather than in a ControlError, which means "nothing ever ran".
    pub timed_out: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ControlError {
    pub request_id: i64,
    pub code: String,
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FinalizeComplete {
    pub request_id: i64,
}

// ------------------------------------------------------------------ framing

pub fn frame(payload: &str) -> Result<Vec<u8>, ProtocolError> {
    let bytes = payload.as_bytes();
    if bytes.len() > MAX_FRAME_BYTES {
        return err("control frame exceeds the maximum size");
    }
    let mut out = Vec::with_capacity(HEADER_SIZE + bytes.len());
    out.extend_from_slice(&(bytes.len() as u32).to_be_bytes());
    out.extend_from_slice(bytes);
    Ok(out)
}

/// Accumulates bytes from the protocol tty and yields whole frames.
///
/// A length header larger than the bound is refused before any allocation:
/// otherwise a peer could ask for an arbitrary amount of guest memory by
/// claiming a frame it never intends to send.
#[derive(Debug, Default)]
pub struct FrameDecoder {
    buffer: Vec<u8>,
}

impl FrameDecoder {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn pending_bytes(&self) -> usize {
        self.buffer.len()
    }

    pub fn feed(&mut self, data: &[u8]) -> Result<Vec<Vec<u8>>, ProtocolError> {
        self.buffer.extend_from_slice(data);
        let mut payloads = Vec::new();
        loop {
            if self.buffer.len() < HEADER_SIZE {
                return Ok(payloads);
            }
            let length = u32::from_be_bytes([
                self.buffer[0],
                self.buffer[1],
                self.buffer[2],
                self.buffer[3],
            ]) as usize;
            if length > MAX_FRAME_BYTES {
                return err("control frame exceeds the maximum size");
            }
            if self.buffer.len() < HEADER_SIZE + length {
                return Ok(payloads);
            }
            payloads.push(self.buffer[HEADER_SIZE..HEADER_SIZE + length].to_vec());
            self.buffer.drain(..HEADER_SIZE + length);
        }
    }
}

// ----------------------------------------------------------------- encoding

fn object(fields: &[(&str, String)]) -> String {
    let mut out = String::from("{");
    for (index, (key, encoded)) in fields.iter().enumerate() {
        if index > 0 {
            out.push(',');
        }
        json::write_string(&mut out, key);
        out.push(':');
        out.push_str(encoded);
    }
    out.push('}');
    out
}

fn quoted(value: &str) -> String {
    let mut out = String::new();
    json::write_string(&mut out, value);
    out
}

pub fn encode_guest_message(message: &GuestMessage) -> Result<Vec<u8>, ProtocolError> {
    let payload = match message {
        GuestMessage::ControllerReady(ready) => object(&[
            ("v", PROTOCOL_VERSION.to_string()),
            ("type", quoted("controller_ready")),
            ("protocol_version", ready.protocol_version.to_string()),
        ]),
        GuestMessage::ExecOutputChunk(chunk) => {
            if chunk.request_id < 0 {
                return err("output chunk request ID is invalid");
            }
            if chunk.data.len() > MAX_OUTPUT_CHUNK_BYTES {
                return err("output chunk exceeds the maximum size");
            }
            object(&[
                ("v", PROTOCOL_VERSION.to_string()),
                ("type", quoted("exec_output")),
                ("request_id", chunk.request_id.to_string()),
                ("stream", quoted(chunk.stream.as_str())),
                ("data_b64", quoted(&base64::encode(&chunk.data))),
            ])
        }
        GuestMessage::ExecComplete(complete) => {
            if complete.request_id < 0 {
                return err("completion request ID is invalid");
            }
            object(&[
                ("v", PROTOCOL_VERSION.to_string()),
                ("type", quoted("exec_complete")),
                ("request_id", complete.request_id.to_string()),
                ("return_code", complete.return_code.to_string()),
                ("timed_out", complete.timed_out.to_string()),
            ])
        }
        GuestMessage::ControlError(error) => {
            if error.request_id < 0 {
                return err("control error request ID is invalid");
            }
            if !CONTROL_ERROR_CODES.contains(&error.code.as_str()) {
                return err("control error code is not a known code");
            }
            if error.message.len() > MAX_ERROR_MESSAGE_BYTES {
                return err("control error message exceeds the maximum size");
            }
            object(&[
                ("v", PROTOCOL_VERSION.to_string()),
                ("type", quoted("control_error")),
                ("request_id", error.request_id.to_string()),
                ("code", quoted(&error.code)),
                ("message", quoted(&error.message)),
            ])
        }
        GuestMessage::FinalizeComplete(complete) => {
            if complete.request_id < 0 {
                return err("finalize completion request ID is invalid");
            }
            object(&[
                ("v", PROTOCOL_VERSION.to_string()),
                ("type", quoted("finalize_complete")),
                ("request_id", complete.request_id.to_string()),
            ])
        }
    };
    frame(&payload)
}

// ----------------------------------------------------------------- decoding

fn require_object(payload: &[u8]) -> Result<BTreeMap<String, Value>, ProtocolError> {
    let text = std::str::from_utf8(payload)
        .map_err(|_| ProtocolError("control frame is not valid UTF-8".into()))?;
    let value = json::parse(text).map_err(ProtocolError)?;
    let map = value
        .as_object()
        .ok_or_else(|| ProtocolError("control message must be a JSON object".into()))?
        .clone();
    match map.get("v").and_then(Value::as_i64) {
        Some(PROTOCOL_VERSION) => {}
        _ => return err("unsupported control protocol version"),
    }
    Ok(map)
}

fn require_keys(map: &BTreeMap<String, Value>, expected: &[&str]) -> Result<(), ProtocolError> {
    if map.len() != expected.len() || !expected.iter().all(|key| map.contains_key(*key)) {
        return err("control message has unexpected fields");
    }
    Ok(())
}

fn require_request_id(map: &BTreeMap<String, Value>) -> Result<i64, ProtocolError> {
    match map.get("request_id").and_then(Value::as_i64) {
        Some(id) if id >= 0 => Ok(id),
        _ => err("request ID is invalid"),
    }
}

/// Decode one host-to-guest frame.
///
/// A guest-direction message arriving here is refused as a direction error
/// rather than decoded: the host is not supposed to be able to make the guest
/// act on a frame the guest itself emits.
pub fn decode_host_message(payload: &[u8]) -> Result<HostMessage, ProtocolError> {
    let map = require_object(payload)?;
    let kind = map
        .get("type")
        .and_then(Value::as_str)
        .ok_or_else(|| ProtocolError("control message has no type".into()))?;

    match kind {
        "exec" => {
            require_keys(
                &map,
                &[
                    "v",
                    "type",
                    "request_id",
                    "argv",
                    "cwd",
                    "env",
                    "timeout_sec",
                    "user",
                ],
            )?;
            let request_id = require_request_id(&map)?;

            let argv_values = map
                .get("argv")
                .and_then(Value::as_array)
                .ok_or_else(|| ProtocolError("exec argv must be an array".into()))?;
            if argv_values.is_empty() {
                return err("exec argv cannot be empty");
            }
            let mut argv = Vec::with_capacity(argv_values.len());
            for item in argv_values {
                let text = item
                    .as_str()
                    .ok_or_else(|| ProtocolError("exec argv items must be strings".into()))?;
                if text.contains('\0') {
                    return err("exec argv item contains a NUL byte");
                }
                argv.push(text.to_string());
            }

            let cwd = match map.get("cwd") {
                Some(Value::Null) => None,
                Some(Value::Str(text)) => {
                    if text.is_empty() {
                        return err("exec cwd cannot be empty");
                    }
                    if text.contains('\0') {
                        return err("exec cwd contains a NUL byte");
                    }
                    Some(text.clone())
                }
                _ => return err("exec cwd must be a string or null"),
            };

            let mut env = BTreeMap::new();
            for (key, value) in map
                .get("env")
                .and_then(Value::as_object)
                .ok_or_else(|| ProtocolError("exec env must be an object".into()))?
            {
                let text = value
                    .as_str()
                    .ok_or_else(|| ProtocolError("exec env values must be strings".into()))?;
                if key.is_empty() || key.contains('=') || key.contains('\0') {
                    return err("exec env name is invalid");
                }
                if text.contains('\0') {
                    return err("exec env value contains a NUL byte");
                }
                env.insert(key.clone(), text.to_string());
            }

            let timeout_sec = match map.get("timeout_sec") {
                Some(Value::Null) => None,
                Some(Value::Int(seconds)) if *seconds > 0 => Some(*seconds),
                _ => return err("exec timeout must be a positive integer or null"),
            };

            let user = match map.get("user") {
                Some(Value::Str(name)) if !name.trim().is_empty() => UserField::Name(name.clone()),
                Some(Value::Int(uid)) if *uid >= 0 => UserField::Uid(*uid),
                _ => return err("exec user is invalid"),
            };

            Ok(HostMessage::Exec(ExecRequest {
                request_id,
                argv,
                cwd,
                env,
                timeout_sec,
                user,
            }))
        }
        "finalize" => {
            require_keys(&map, &["v", "type", "request_id"])?;
            Ok(HostMessage::Finalize(FinalizeRequest {
                request_id: require_request_id(&map)?,
            }))
        }
        "controller_ready" | "exec_output" | "exec_complete" | "control_error"
        | "finalize_complete" => err("guest-direction message arrived from the host"),
        _ => err("unknown control message type"),
    }
}

/// Decode one guest-to-host frame. Used by the cross-language tests and by any
/// host-side tooling written in Rust; the controller itself only encodes these.
pub fn decode_guest_message(payload: &[u8]) -> Result<GuestMessage, ProtocolError> {
    let map = require_object(payload)?;
    let kind = map
        .get("type")
        .and_then(Value::as_str)
        .ok_or_else(|| ProtocolError("control message has no type".into()))?;

    match kind {
        "controller_ready" => {
            require_keys(&map, &["v", "type", "protocol_version"])?;
            let protocol_version = map
                .get("protocol_version")
                .and_then(Value::as_i64)
                .ok_or_else(|| ProtocolError("controller protocol version is invalid".into()))?;
            Ok(GuestMessage::ControllerReady(ControllerReady {
                protocol_version,
            }))
        }
        "exec_output" => {
            require_keys(&map, &["v", "type", "request_id", "stream", "data_b64"])?;
            let request_id = require_request_id(&map)?;
            let stream = match map.get("stream").and_then(Value::as_str) {
                Some("stdout") => Stream::Stdout,
                Some("stderr") => Stream::Stderr,
                _ => return err("output chunk stream is invalid"),
            };
            let encoded = map
                .get("data_b64")
                .and_then(Value::as_str)
                .ok_or_else(|| ProtocolError("output chunk data must be a string".into()))?;
            let data = base64::decode(encoded)
                .map_err(|_| ProtocolError("output chunk contains invalid base64".into()))?;
            if data.len() > MAX_OUTPUT_CHUNK_BYTES {
                return err("output chunk exceeds the maximum size");
            }
            Ok(GuestMessage::ExecOutputChunk(ExecOutputChunk {
                request_id,
                stream,
                data,
            }))
        }
        "exec_complete" => {
            require_keys(
                &map,
                &["v", "type", "request_id", "return_code", "timed_out"],
            )?;
            let request_id = require_request_id(&map)?;
            let return_code = map
                .get("return_code")
                .and_then(Value::as_i64)
                .ok_or_else(|| ProtocolError("completion return code must be an integer".into()))?;
            let timed_out = map
                .get("timed_out")
                .and_then(Value::as_bool)
                .ok_or_else(|| ProtocolError("completion timed_out must be a boolean".into()))?;
            Ok(GuestMessage::ExecComplete(ExecComplete {
                request_id,
                return_code,
                timed_out,
            }))
        }
        "control_error" => {
            require_keys(&map, &["v", "type", "request_id", "code", "message"])?;
            let request_id = require_request_id(&map)?;
            let code = map
                .get("code")
                .and_then(Value::as_str)
                .filter(|code| CONTROL_ERROR_CODES.contains(code))
                .ok_or_else(|| ProtocolError("control error code is not a known code".into()))?;
            let message = map
                .get("message")
                .and_then(Value::as_str)
                .ok_or_else(|| ProtocolError("control error message must be a string".into()))?;
            if message.len() > MAX_ERROR_MESSAGE_BYTES {
                return err("control error message exceeds the maximum size");
            }
            Ok(GuestMessage::ControlError(ControlError {
                request_id,
                code: code.to_string(),
                message: message.to_string(),
            }))
        }
        "finalize_complete" => {
            require_keys(&map, &["v", "type", "request_id"])?;
            Ok(GuestMessage::FinalizeComplete(FinalizeComplete {
                request_id: require_request_id(&map)?,
            }))
        }
        "exec" | "finalize" => err("host-direction message arrived from the guest"),
        _ => err("unknown control message type"),
    }
}
