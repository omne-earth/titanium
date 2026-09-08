//! Becoming the standard principal, permanently, before any host byte is read.
//!
//! The controller may start with enough privilege to set its own credentials,
//! because no unit directive can subtract the supplementary groups an image's
//! account database confers: `systemd.exec(5)` says `SupplementaryGroups=`
//! "does not override, but extends the list of supplementary groups configured
//! in the system group database for the user". A guest whose UID 1000 is
//! listed in the image's own `sudo` or `docker` group would otherwise carry
//! those memberships into every workload.
//!
//! So the drop happens here, in this order, and the order is the security
//! property:
//!
//! 1. `setgroups([])` — must come first; it needs the privilege that step 3
//!    gives away, and running it afterwards would silently fail.
//! 2. `setresgid(gid, gid, gid)` — before the uid change, for the same reason.
//! 3. `setresuid(1000, 1000, 1000)` — all three ids, so there is no saved uid
//!    to return to. This is what makes the drop irreversible.
//! 4. `PR_SET_NO_NEW_PRIVS` — inherited by every descendant and impossible to
//!    unset, so no workload can regain privilege through a setuid binary.
//! 5. `PR_SET_DUMPABLE(0)` — the controller holds the protocol descriptors;
//!    a dumpable process would let a same-uid workload read them out of a core
//!    or attach to `/proc/<pid>/mem`.
//!
//! Then the result is verified by reading `/proc/self/status` rather than by
//! trusting the return values just collected: if any step silently did nothing,
//! the check has to notice.
//!
//! The target comes from the boot configuration Titanium generated, never from
//! a request. Nothing on the wire can influence who the controller becomes.

use std::fmt;

pub const STANDARD_PRINCIPAL_UID: u32 = 1000;

const PR_GET_DUMPABLE: i32 = 3;
const PR_SET_DUMPABLE: i32 = 4;
const PR_SET_NO_NEW_PRIVS: i32 = 38;

extern "C" {
    fn setgroups(size: usize, list: *const u32) -> i32;
    fn setresgid(rgid: u32, egid: u32, sgid: u32) -> i32;
    fn setresuid(ruid: u32, euid: u32, suid: u32) -> i32;
    fn prctl(option: i32, arg2: u64, arg3: u64, arg4: u64, arg5: u64) -> i32;
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TargetPrincipal {
    pub uid: u32,
    pub gid: u32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CredentialError {
    /// The target itself is not a principal the controller may become.
    Target(String),
    /// A syscall in the drop sequence failed.
    Syscall(&'static str, i32),
    /// `/proc/self/status` could not be read or understood.
    Status(String),
    /// The drop appeared to succeed and did not.
    NotDropped(String),
}

impl fmt::Display for CredentialError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CredentialError::Target(detail) => write!(f, "invalid target principal: {detail}"),
            CredentialError::Syscall(name, code) => write!(f, "{name} failed with errno {code}"),
            CredentialError::Status(detail) => write!(f, "reading /proc/self/status: {detail}"),
            CredentialError::NotDropped(detail) => {
                write!(f, "credentials were not dropped: {detail}")
            }
        }
    }
}

impl std::error::Error for CredentialError {}

/// What the kernel says this process's credentials actually are.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcCredentials {
    /// real, effective, saved, filesystem.
    pub uid: [u32; 4],
    pub gid: [u32; 4],
    pub groups: Vec<u32>,
    pub no_new_privs: bool,
    /// Read with `PR_GET_DUMPABLE`, not from `/proc`: this kernel's
    /// `/proc/<pid>/status` has no such field, and a missing line would read
    /// as "not dumpable" exactly when the check matters most.
    pub dumpable: bool,
}

fn errno() -> i32 {
    std::io::Error::last_os_error().raw_os_error().unwrap_or(-1)
}

fn parse_ids(line: &str) -> Option<[u32; 4]> {
    let mut values = line
        .split_whitespace()
        .filter_map(|v| v.parse::<u32>().ok());
    Some([
        values.next()?,
        values.next()?,
        values.next()?,
        values.next()?,
    ])
}

pub fn read_proc_credentials() -> Result<ProcCredentials, CredentialError> {
    let text = std::fs::read_to_string("/proc/self/status")
        .map_err(|e| CredentialError::Status(e.to_string()))?;
    let mut uid = None;
    let mut gid = None;
    let mut groups = None;
    let mut no_new_privs = None;
    for line in text.lines() {
        if let Some(rest) = line.strip_prefix("Uid:") {
            uid = parse_ids(rest);
        } else if let Some(rest) = line.strip_prefix("Gid:") {
            gid = parse_ids(rest);
        } else if let Some(rest) = line.strip_prefix("Groups:") {
            groups = Some(
                rest.split_whitespace()
                    .filter_map(|v| v.parse::<u32>().ok())
                    .collect::<Vec<u32>>(),
            );
        } else if let Some(rest) = line.strip_prefix("NoNewPrivs:") {
            no_new_privs = rest.trim().parse::<u32>().ok().map(|v| v == 1);
        }
    }
    // SAFETY: PR_GET_DUMPABLE takes no pointers and only reads this process.
    let dumpable = unsafe { prctl(PR_GET_DUMPABLE, 0, 0, 0, 0) };
    if dumpable < 0 {
        return Err(CredentialError::Status("PR_GET_DUMPABLE failed".into()));
    }

    Ok(ProcCredentials {
        dumpable: dumpable != 0,
        uid: uid.ok_or_else(|| CredentialError::Status("no Uid line".into()))?,
        gid: gid.ok_or_else(|| CredentialError::Status("no Gid line".into()))?,
        groups: groups.ok_or_else(|| CredentialError::Status("no Groups line".into()))?,
        no_new_privs: no_new_privs
            .ok_or_else(|| CredentialError::Status("no NoNewPrivs line".into()))?,
    })
}

/// Reject a target that is not the standard principal before touching anything.
pub fn check_target(target: TargetPrincipal) -> Result<(), CredentialError> {
    if target.uid != STANDARD_PRINCIPAL_UID {
        return Err(CredentialError::Target(format!(
            "uid must be {STANDARD_PRINCIPAL_UID}, got {}",
            target.uid
        )));
    }
    if target.gid == 0 {
        return Err(CredentialError::Target(
            "primary group must not be the root group".into(),
        ));
    }
    Ok(())
}

/// Confirm the process now holds exactly *target* and nothing more.
///
/// Read back from the kernel rather than inferred: every one of these is a
/// property a later reader of this code would otherwise have to take on faith.
pub fn verify_principal(target: TargetPrincipal) -> Result<ProcCredentials, CredentialError> {
    let creds = read_proc_credentials()?;
    let fail = |detail: String| Err(CredentialError::NotDropped(detail));

    if creds.uid != [target.uid; 4] {
        return fail(format!(
            "uid is {:?}, expected all {}",
            creds.uid, target.uid
        ));
    }
    if creds.gid != [target.gid; 4] {
        return fail(format!(
            "gid is {:?}, expected all {}",
            creds.gid, target.gid
        ));
    }
    if target.gid == 0 {
        return fail("primary group is the root group".into());
    }
    if !creds.groups.is_empty() {
        return fail(format!("supplementary groups remain: {:?}", creds.groups));
    }
    if !creds.no_new_privs {
        return fail("NoNewPrivs is not set".into());
    }
    // The controller holds the protocol descriptors. A dumpable process lets
    // a same-uid workload read them out of a core file or through
    // /proc/<pid>/mem, which would let it forge frames to the host.
    if creds.dumpable {
        return fail("the process is still dumpable".into());
    }
    Ok(creds)
}

/// Become *target*, irreversibly, and prove it.
///
/// On any error the caller must not announce readiness and must not read a
/// request: a controller that failed to shed privilege has nothing safe to do.
pub fn become_principal(target: TargetPrincipal) -> Result<ProcCredentials, CredentialError> {
    check_target(target)?;

    // SAFETY: each call is a plain credential syscall with no memory
    // borrowed beyond the call. The null list is how setgroups spells "none".
    unsafe {
        if setgroups(0, std::ptr::null()) != 0 {
            return Err(CredentialError::Syscall("setgroups", errno()));
        }
        if setresgid(target.gid, target.gid, target.gid) != 0 {
            return Err(CredentialError::Syscall("setresgid", errno()));
        }
        if setresuid(target.uid, target.uid, target.uid) != 0 {
            return Err(CredentialError::Syscall("setresuid", errno()));
        }
        if prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0 {
            return Err(CredentialError::Syscall("PR_SET_NO_NEW_PRIVS", errno()));
        }
        if prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0 {
            return Err(CredentialError::Syscall("PR_SET_DUMPABLE", errno()));
        }
    }

    verify_principal(target)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_target_that_is_not_the_standard_principal_is_refused() {
        assert!(check_target(TargetPrincipal { uid: 0, gid: 0 }).is_err());
        assert!(check_target(TargetPrincipal {
            uid: 1001,
            gid: 1001
        })
        .is_err());
    }

    #[test]
    fn the_root_group_is_refused_as_a_primary_group() {
        let error = check_target(TargetPrincipal { uid: 1000, gid: 0 }).unwrap_err();
        assert!(format!("{error}").contains("root group"));
    }

    #[test]
    fn an_ordinary_target_is_accepted() {
        assert!(check_target(TargetPrincipal {
            uid: 1000,
            gid: 1234
        })
        .is_ok());
    }

    #[test]
    fn the_status_reader_agrees_with_the_process_it_runs_in() {
        let creds = read_proc_credentials().unwrap();
        // Real uid from /proc must match what the process actually has.
        assert_eq!(creds.uid[0], unsafe { libc_getuid() });
    }

    #[test]
    fn verification_fails_when_nothing_was_dropped() {
        // The test process is not the standard principal, so this must refuse
        // rather than report success.
        let result = verify_principal(TargetPrincipal {
            uid: 1000,
            gid: 1234,
        });
        if let Ok(creds) = &result {
            // Only reachable if the suite really runs as 1000:1234 with no
            // groups and NoNewPrivs already set, which is not a normal shell.
            assert!(creds.groups.is_empty());
        } else {
            assert!(matches!(result, Err(CredentialError::NotDropped(_))));
        }
    }

    extern "C" {
        #[link_name = "getuid"]
        fn libc_getuid() -> u32;
    }
}
