//! The controller's state machine seam.
//!
//! Everything the decision needs is available here as data and helpers; the
//! decision itself is not made in this module. `dispatch` is the single point
//! at which a decoded host message becomes an action, and it currently refuses
//! every message rather than guessing at one.
//!
//! The one rule this module does enforce structurally: `Finalized` and
//! `Finalizing` are terminal with respect to ordinary work. There is no
//! transition back to `Running` from either, because "pencils down" cannot be
//! revoked by a later failure to flush.

use crate::protocol::{ControlError, GuestMessage, HostMessage};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ControllerState {
    /// Ordinary execution is accepted.
    Running,
    /// A finalize request has been accepted. No further exec is served, and
    /// there is no path from here back to `Running`.
    Finalizing,
    /// Quiescence and syncfs both succeeded, and the host may freeze.
    Finalized,
}

impl ControllerState {
    /// Whether ordinary execution may still be served.
    pub fn accepts_exec(&self) -> bool {
        matches!(self, ControllerState::Running)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ControllerError {
    /// The dispatch policy has not been written yet. Returned instead of a
    /// panic so that a controller built from this tree fails one request
    /// visibly rather than taking the process down and leaving the host
    /// looking at a closed tty it cannot explain.
    DispatchNotImplemented,
    /// The peer sent something the controller will not act on. The connection
    /// is no longer trustworthy.
    Protocol(String),
    /// A refusal the host can be told about in band.
    Reported(ControlError),
}

impl std::fmt::Display for ControllerError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ControllerError::DispatchNotImplemented => {
                write!(f, "dispatch policy is not implemented")
            }
            ControllerError::Protocol(detail) => write!(f, "protocol failure: {detail}"),
            ControllerError::Reported(error) => {
                write!(f, "{}: {}", error.code, error.message)
            }
        }
    }
}

impl std::error::Error for ControllerError {}

/// What `dispatch` may do to the outside world.
///
/// A trait rather than a concrete type so the policy can be exercised against
/// a recording double: the decision under test is which calls are made in
/// which order, not whether a real process was spawned.
pub trait ControllerContext {
    /// Write one frame back to the host.
    fn send(&mut self, message: GuestMessage) -> Result<(), ControllerError>;

    /// Run one request to completion, streaming its output through `send`.
    ///
    /// Precondition: the state accepts exec, and the request's identity, cwd
    /// and environment have already been resolved and accepted.
    /// Postcondition: exactly one terminal frame has been sent for this
    /// request id -- an `ExecComplete` if a program ran, a `ControlError` if
    /// none could be started.
    fn run_exec(&mut self, request: crate::protocol::ExecRequest) -> Result<(), ControllerError>;

    /// Stop every workload process, then make the filesystem durable.
    ///
    /// Postcondition on success: no workload process is runnable and `syncfs`
    /// has returned. On failure the controller must not return to `Running`.
    fn quiesce_and_sync(&mut self) -> Result<(), ControllerError>;
}

/// Turn one decoded host message into action.
///
/// Left unimplemented on purpose: the ordering rules between exec, finalize
/// and the terminal states are the load-bearing part of the controller, and
/// they are written once, deliberately, rather than inferred from a scaffold.
pub fn dispatch(
    _state: &mut ControllerState,
    _message: HostMessage,
    _ctx: &mut dyn ControllerContext,
) -> Result<(), ControllerError> {
    Err(ControllerError::DispatchNotImplemented)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::{ExecRequest, FinalizeRequest, UserField};
    use std::collections::BTreeMap;

    #[derive(Default)]
    struct Recorder {
        sent: Vec<GuestMessage>,
        execs: usize,
        quiesces: usize,
    }

    impl ControllerContext for Recorder {
        fn send(&mut self, message: GuestMessage) -> Result<(), ControllerError> {
            self.sent.push(message);
            Ok(())
        }
        fn run_exec(&mut self, _request: ExecRequest) -> Result<(), ControllerError> {
            self.execs += 1;
            Ok(())
        }
        fn quiesce_and_sync(&mut self) -> Result<(), ControllerError> {
            self.quiesces += 1;
            Ok(())
        }
    }

    fn an_exec() -> HostMessage {
        HostMessage::Exec(ExecRequest {
            request_id: 1,
            argv: vec!["/bin/true".into()],
            cwd: None,
            env: BTreeMap::new(),
            timeout_sec: None,
            user: UserField::Uid(1000),
        })
    }

    #[test]
    fn dispatch_is_fail_closed_until_it_is_written() {
        let mut state = ControllerState::Running;
        let mut ctx = Recorder::default();
        assert_eq!(
            dispatch(&mut state, an_exec(), &mut ctx),
            Err(ControllerError::DispatchNotImplemented)
        );
    }

    #[test]
    fn the_unimplemented_dispatch_performs_no_action() {
        let mut state = ControllerState::Running;
        let mut ctx = Recorder::default();
        let _ = dispatch(&mut state, an_exec(), &mut ctx);
        let _ = dispatch(
            &mut state,
            HostMessage::Finalize(FinalizeRequest { request_id: 2 }),
            &mut ctx,
        );
        assert!(ctx.sent.is_empty());
        assert_eq!(ctx.execs, 0);
        assert_eq!(ctx.quiesces, 0);
        // And it does not move the state on its own.
        assert_eq!(state, ControllerState::Running);
    }

    #[test]
    fn only_running_accepts_exec() {
        assert!(ControllerState::Running.accepts_exec());
        assert!(!ControllerState::Finalizing.accepts_exec());
        assert!(!ControllerState::Finalized.accepts_exec());
    }
}
