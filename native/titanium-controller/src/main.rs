//! Entry point.
//!
//! The startup sequence is fixed and its order is the security property: the
//! process becomes the standard principal, irreversibly, before it reads a
//! single byte from the host. Only then does it announce readiness, and only
//! after that does it accept a request.
//!
//! The dispatch loop that follows is not wired yet.

use std::process::ExitCode;

use titanium_controller::credentials::{become_principal, TargetPrincipal};

fn selftest(args: &[String]) -> ExitCode {
    // Becomes the given principal in this process and prints what the kernel
    // says afterwards, so the drop can be proven from outside rather than
    // asserted from within. Meant to be run inside a throwaway user namespace.
    let Some(uid) = args.first().and_then(|v| v.parse::<u32>().ok()) else {
        eprintln!("usage: --selftest-credentials <uid> <gid>");
        return ExitCode::FAILURE;
    };
    let Some(gid) = args.get(1).and_then(|v| v.parse::<u32>().ok()) else {
        eprintln!("usage: --selftest-credentials <uid> <gid>");
        return ExitCode::FAILURE;
    };

    match become_principal(TargetPrincipal { uid, gid }) {
        Ok(creds) => {
            let groups: Vec<String> = creds.groups.iter().map(u32::to_string).collect();
            println!("uid={:?}", creds.uid);
            println!("gid={:?}", creds.gid);
            println!("groups=[{}]", groups.join(","));
            println!("no_new_privs={}", creds.no_new_privs);
            println!("dumpable={}", creds.dumpable);
            println!("OK");
            ExitCode::SUCCESS
        }
        Err(error) => {
            println!("FAILED {error}");
            ExitCode::FAILURE
        }
    }
}

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    if argv.first().map(String::as_str) == Some("--selftest-credentials") {
        return selftest(&argv[1..]);
    }

    eprintln!(
        "titanium-controller {}: the dispatch loop is not wired yet.",
        env!("CARGO_PKG_VERSION")
    );
    ExitCode::FAILURE
}
