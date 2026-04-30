fn main() {
    // DuckDB 1.4+ uses the Windows Restart Manager API, which lives in rstrtmgr.lib.
    // Without this, linking fails with unresolved symbols for RmStartSession, etc.
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        println!("cargo:rustc-link-lib=rstrtmgr");
    }
}
