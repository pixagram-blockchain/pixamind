//! pixamind-api: Pixamind's JSON-RPC and REST API, served straight from the
//! API's SQL functions. It replaces PostgREST and the nginx rewriter in front
//! of it with one process and keeps their behaviour: the same paths, the same
//! bodies, the same error shapes and status codes.

mod config;
mod db;
mod error;
mod metrics;
mod rewrite;
mod rpc;
mod schema;
mod server;

use std::sync::Arc;
use std::time::Duration;

use anyhow::Context;
use clap::Parser;
use tokio::net::TcpListener;
use tokio::sync::watch;
use tracing_subscriber::EnvFilter;

use crate::config::{Config, LogFormat};
use crate::rewrite::RewriteRules;
use crate::server::AppState;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let config = Config::parse();
    if config.healthcheck {
        std::process::exit(if healthcheck(&config).await { 0 } else { 1 });
    }
    init_logging(&config);

    let rules_text = match &config.rewrite_rules {
        Some(path) => std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?,
        None => rewrite::BUNDLED.to_string(),
    };
    let rules = RewriteRules::parse(&rules_text).map_err(|e| anyhow::anyhow!("rewrite rules: {e}"))?;
    let pool = db::create_pool(&config)?;
    let state = Arc::new(AppState::new(config.clone(), rules, pool));

    let api_listener = TcpListener::bind(config.listen).await.with_context(|| format!("binding {}", config.listen))?;
    let admin_listener = match config.admin_listen.0 {
        Some(addr) => Some(TcpListener::bind(addr).await.with_context(|| format!("binding {addr}"))?),
        None => None,
    };
    tracing::info!(
        listen = %config.listen,
        admin = ?config.admin_listen.0,
        schema = %config.schema,
        role = %config.role,
        rules = state.rules.len(),
        version = env!("CARGO_PKG_VERSION"),
        "pixamind-api started"
    );

    tokio::spawn(keep_schema_fresh(state.clone()));
    #[cfg(unix)]
    tokio::spawn(reload_on_sigusr1(state.clone()));

    let (stop_tx, stop_rx) = watch::channel(false);
    tokio::spawn(async move {
        shutdown_signal().await;
        tracing::info!("shutting down");
        let _ = stop_tx.send(true);
    });

    let api =
        axum::serve(api_listener, server::api_router(state.clone())).with_graceful_shutdown(stopped(stop_rx.clone()));
    match admin_listener {
        Some(listener) => {
            let admin =
                axum::serve(listener, server::admin_router(state.clone())).with_graceful_shutdown(stopped(stop_rx));
            tokio::try_join!(api.into_future(), admin.into_future())?;
        }
        None => api.await?,
    }
    Ok(())
}

/// GET /ready on the admin listener, without an HTTP client library.
async fn healthcheck(config: &Config) -> bool {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let Some(mut addr) = config.admin_listen.0 else { return false };
    if addr.ip().is_unspecified() {
        let localhost: std::net::IpAddr =
            if addr.is_ipv4() { std::net::Ipv4Addr::LOCALHOST.into() } else { std::net::Ipv6Addr::LOCALHOST.into() };
        addr.set_ip(localhost);
    }
    let check = async {
        let mut stream = tokio::net::TcpStream::connect(addr).await.ok()?;
        stream.write_all(b"GET /ready HTTP/1.0\r\nHost: localhost\r\n\r\n").await.ok()?;
        let mut head = [0u8; 16];
        stream.read_exact(&mut head).await.ok()?;
        let status = std::str::from_utf8(&head).ok()?;
        Some(status.starts_with("HTTP/1.1 200") || status.starts_with("HTTP/1.0 200"))
    };
    matches!(tokio::time::timeout(Duration::from_secs(5), check).await, Ok(Some(true)))
}

fn init_logging(config: &Config) {
    let filter = EnvFilter::try_new(&config.log).unwrap_or_else(|_| EnvFilter::new("info"));
    let builder = tracing_subscriber::fmt().with_env_filter(filter);
    match config.log_format {
        LogFormat::Json => builder.json().init(),
        LogFormat::Text => builder.init(),
    }
}

/// Load the function list at start (retrying until the database answers),
/// then re-read it periodically: the indexer reinstalls the SQL on upgrades.
async fn keep_schema_fresh(state: Arc<AppState>) {
    let mut delay = Duration::from_secs(1);
    while !state.schema().is_loaded() {
        match state.reload_schema(Duration::ZERO).await {
            Ok(()) => tracing::info!(functions = state.schema().count(), "schema cache loaded"),
            Err(e) => {
                tracing::warn!("schema cache not loaded yet: {} {}", e.message, e.details.unwrap_or_default());
                tokio::time::sleep(delay).await;
                delay = (delay * 2).min(Duration::from_secs(30));
            }
        }
    }
    let period = state.config.schema_refresh;
    if period.is_zero() {
        return;
    }
    let mut ticker = tokio::time::interval(period);
    ticker.tick().await;
    loop {
        ticker.tick().await;
        if let Err(e) = state.reload_schema(Duration::ZERO).await {
            tracing::warn!("schema cache refresh failed: {}", e.message);
        }
    }
}

/// `kill -USR1` re-reads the function list, like PostgREST.
#[cfg(unix)]
async fn reload_on_sigusr1(state: Arc<AppState>) {
    use tokio::signal::unix::{SignalKind, signal};
    let Ok(mut usr1) = signal(SignalKind::user_defined1()) else { return };
    while usr1.recv().await.is_some() {
        match state.reload_schema(Duration::ZERO).await {
            Ok(()) => tracing::info!(functions = state.schema().count(), "schema cache reloaded"),
            Err(e) => tracing::warn!("schema cache reload failed: {}", e.message),
        }
    }
}

async fn shutdown_signal() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    #[cfg(unix)]
    let terminate = async {
        match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
            Ok(mut s) => {
                s.recv().await;
            }
            Err(_) => std::future::pending::<()>().await,
        }
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();
    tokio::select! {
        () = ctrl_c => {},
        () = terminate => {},
    }
}

async fn stopped(mut rx: watch::Receiver<bool>) {
    while !*rx.borrow() {
        if rx.changed().await.is_err() {
            return;
        }
    }
}
