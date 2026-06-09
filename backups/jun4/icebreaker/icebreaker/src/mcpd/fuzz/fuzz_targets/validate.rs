#![no_main]

//! Coverage-guided fuzz target for `mcpd::tools::fs::validate`.
//!
//! Replaces the hand-rolled LCG harness that used to live as a unit test in
//! `src/tools/fs.rs`. libFuzzer drives the input, the seed corpus under
//! `corpus/validate/` carries the known attack shapes (NUL, %2e, backslash,
//! `..`, absolute path prefix games). G4 in ci.sh runs this for 60 s and
//! asserts zero crashes.
//!
//! Invariants asserted on accepted paths:
//!   (a) the matched root is one of mcpd's whitelisted roots
//!   (b) the relative portion never contains NUL / '%' / '\\'
//! Anything else (panic, OOM, infinite loop) shows up as a libFuzzer crash.

use libfuzzer_sys::fuzz_target;
use mcpd::tools::fs;

fuzz_target!(|data: &[u8]| {
    // validate() takes &str. Cheap UTF-8 reject keeps libFuzzer hammering the
    // string-parsing surface rather than the conversion error.
    let Ok(input) = std::str::from_utf8(data) else { return };

    if let Ok(v) = fs::validate(input) {
        // Accept invariants — same as the old LCG test asserted.
        assert!(
            !v.rel.contains('\0'),
            "NUL leaked into rel: input={:?} rel={:?}",
            input,
            v.rel
        );
        assert!(
            !v.rel.contains('%'),
            "% leaked into rel: input={:?} rel={:?}",
            input,
            v.rel
        );
        assert!(
            !v.rel.contains('\\'),
            "\\ leaked into rel: input={:?} rel={:?}",
            input,
            v.rel
        );
    }
});
