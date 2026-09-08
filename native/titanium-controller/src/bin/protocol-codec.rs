//! A test instrument: the protocol, driven from stdin.
//!
//! Exists so the host's Python implementation and the guest's Rust one can be
//! proven to agree on bytes rather than assumed to, by round-tripping real
//! frames through the other language. Not part of the controller itself.
//!
//!   emit                  write every canonical guest frame, one hex line each
//!   roundtrip             read guest frames as hex, decode, re-encode, write hex
//!   describe              read host frames as hex, decode, write a normalized form
//!
//! A frame that fails to decode is reported as `ERROR <detail>` on its own
//! line, so a negative test can assert the refusal rather than a crash.

use std::io::{self, Read, Write};

use titanium_controller::protocol::{
    decode_guest_message, encode_guest_message, ControlError, ControllerReady, ExecComplete,
    ExecOutputChunk, FinalizeComplete, GuestMessage, HostMessage, Stream, UserField,
};

fn hex_encode(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn hex_decode(text: &str) -> Result<Vec<u8>, String> {
    let text = text.trim();
    if !text.len().is_multiple_of(2) {
        return Err("odd-length hex".into());
    }
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).map_err(|e| e.to_string()))
        .collect()
}

fn canonical_guest_messages() -> Vec<GuestMessage> {
    vec![
        GuestMessage::ControllerReady(ControllerReady {
            protocol_version: 1,
        }),
        GuestMessage::ExecOutputChunk(ExecOutputChunk {
            request_id: 7,
            stream: Stream::Stdout,
            // NUL, a lone 0xff, and a UTF-8 snowman: none of these survive a
            // text-shaped transport, which is why output is base64.
            data: vec![0x00, 0x01, 0xff, 0xfe, 0xe2, 0x98, 0x83, 0x0a],
        }),
        GuestMessage::ExecOutputChunk(ExecOutputChunk {
            request_id: 7,
            stream: Stream::Stderr,
            data: b"warning\n".to_vec(),
        }),
        GuestMessage::ExecComplete(ExecComplete {
            request_id: 7,
            return_code: 0,
            timed_out: false,
        }),
        GuestMessage::ExecComplete(ExecComplete {
            request_id: 8,
            return_code: 137,
            timed_out: true,
        }),
        GuestMessage::ControlError(ControlError {
            request_id: 9,
            code: "cwd_unavailable".into(),
            message: "no such directory: /nope \"quoted\" \\ backslash".into(),
        }),
        GuestMessage::FinalizeComplete(FinalizeComplete { request_id: 10 }),
    ]
}

fn describe(message: &HostMessage) -> String {
    match message {
        HostMessage::Exec(request) => {
            let env: Vec<String> = request
                .env
                .iter()
                .map(|(k, v)| format!("{k}={v}"))
                .collect();
            let user = match &request.user {
                UserField::Name(name) => format!("name:{name}"),
                UserField::Uid(uid) => format!("uid:{uid}"),
            };
            format!(
                "exec id={} argv={} cwd={} env={} timeout={} user={}",
                request.request_id,
                request.argv.join("\u{1f}"),
                request.cwd.clone().unwrap_or_else(|| "<none>".into()),
                env.join("\u{1f}"),
                request
                    .timeout_sec
                    .map(|s| s.to_string())
                    .unwrap_or_else(|| "<none>".into()),
                user,
            )
        }
        HostMessage::Finalize(request) => format!("finalize id={}", request.request_id),
    }
}

fn main() {
    let mode = std::env::args().nth(1).unwrap_or_default();
    let stdout = io::stdout();
    let mut out = stdout.lock();

    if mode == "emit" {
        for message in canonical_guest_messages() {
            let frame = encode_guest_message(&message).expect("canonical message encodes");
            writeln!(out, "{}", hex_encode(&frame)).unwrap();
        }
        return;
    }

    let mut input = String::new();
    io::stdin().read_to_string(&mut input).unwrap();

    for line in input.lines().filter(|line| !line.trim().is_empty()) {
        let bytes = match hex_decode(line) {
            Ok(bytes) => bytes,
            Err(detail) => {
                writeln!(out, "ERROR {detail}").unwrap();
                continue;
            }
        };
        // The instrument is given whole frames; strip the length header the
        // same way the decoder loop would.
        if bytes.len() < 4 {
            writeln!(out, "ERROR short frame").unwrap();
            continue;
        }
        let length = u32::from_be_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]) as usize;
        if bytes.len() != 4 + length {
            writeln!(out, "ERROR frame length mismatch").unwrap();
            continue;
        }
        let payload = &bytes[4..];

        match mode.as_str() {
            "roundtrip" => match decode_guest_message(payload) {
                Ok(message) => match encode_guest_message(&message) {
                    Ok(frame) => writeln!(out, "{}", hex_encode(&frame)).unwrap(),
                    Err(error) => writeln!(out, "ERROR {error}").unwrap(),
                },
                Err(error) => writeln!(out, "ERROR {error}").unwrap(),
            },
            "describe" => match titanium_controller::protocol::decode_host_message(payload) {
                Ok(message) => writeln!(out, "{}", describe(&message)).unwrap(),
                Err(error) => writeln!(out, "ERROR {error}").unwrap(),
            },
            other => {
                writeln!(out, "ERROR unknown mode {other}").unwrap();
            }
        }
    }
}
