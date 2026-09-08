//! The Titanium guest controller.
//!
//! Runs inside the task guest as a non-root service, speaks the control
//! protocol over the raw serial line the VMM exposes, and is the only thing in
//! the guest that starts workload processes.

pub mod base64;
pub mod credentials;
pub mod dispatch;
pub mod json;
pub mod protocol;
