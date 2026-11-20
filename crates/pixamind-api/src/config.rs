//! Command line and environment configuration.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::time::Duration;

use clap::builder::BoolishValueParser;
use clap::{ArgAction, Parser, ValueEnum};

const DEFAULT_DATABASE_URL: &str = "postgresql://hivemind@localhost:5432/haf_block_log";

/// Pixamind's JSON-RPC and REST API server. It calls the API's SQL functions
/// directly, in place of PostgREST and the nginx rewriter in front of it.
#[derive(Parser, Debug, Clone)]
#[command(name = "pixamind-api", version)]
pub struct Config {
    /// PostgreSQL URL of the API role [default: $POSTGRES_URL, else postgresql://hivemind@localhost:5432/haf_block_log]
    #[arg(long, alias = "postgres-url", env = "PIXAMIND_API_DATABASE_URL")]
    pub database_url: Option<String>,

    /// Address of the API listener
    #[arg(long, env = "PIXAMIND_API_LISTEN", default_value = "0.0.0.0:8080")]
    pub listen: SocketAddr,

    /// Address of the admin listener (/health, /live, /ready, /metrics), or "off"
    #[arg(long, env = "PIXAMIND_API_ADMIN_LISTEN", default_value = "0.0.0.0:3001", value_parser = parse_listen)]
    pub admin_listen: Listen,

    /// Schema whose functions are served (PostgREST: db-schemas)
    #[arg(long, env = "PIXAMIND_API_SCHEMA", default_value = "hivemind_endpoints")]
    pub schema: String,

    /// Role the API queries run as (PostgREST: db-anon-role); empty keeps the login role
    #[arg(long, env = "PIXAMIND_API_ROLE", default_value = "hivemind")]
    pub role: String,

    /// Function answering requests to "/" (PostgREST: db-root-spec)
    #[arg(long, env = "PIXAMIND_API_ROOT_FUNCTION", default_value = "home")]
    pub root_function: String,

    /// nginx rewrite rules mapping public paths to functions [default: the bundled rewrite_rules.conf]
    #[arg(long, env = "PIXAMIND_API_REWRITE_RULES")]
    pub rewrite_rules: Option<PathBuf>,

    /// Database connections (PostgREST: db-pool)
    #[arg(long, env = "PIXAMIND_API_POOL_SIZE", default_value_t = 20)]
    pub pool_size: usize,

    /// How long a request waits for a free connection (PostgREST: db-pool-acquisition-timeout)
    #[arg(long, env = "PIXAMIND_API_POOL_TIMEOUT", default_value = "10s", value_parser = parse_duration)]
    pub pool_timeout: Duration,

    /// Requests running longer are cancelled and answered with 504
    #[arg(long, env = "PIXAMIND_API_REQUEST_TIMEOUT", default_value = "60s", value_parser = parse_duration)]
    pub request_timeout: Duration,

    /// statement_timeout of the API connections [default: the role's setting]
    #[arg(long, env = "PIXAMIND_API_STATEMENT_TIMEOUT", value_parser = parse_duration)]
    pub statement_timeout: Option<Duration>,

    /// Run every query in a read-only transaction (the API functions only read)
    #[arg(long, env = "PIXAMIND_API_READ_ONLY", default_value = "true", action = ArgAction::Set, value_parser = BoolishValueParser::new())]
    pub read_only: bool,

    /// Largest accepted request body, in bytes (nginx's default is 1 MiB)
    #[arg(long, env = "PIXAMIND_API_MAX_BODY", default_value_t = 1024 * 1024)]
    pub max_body_bytes: usize,

    /// Answer CORS requests from any origin, like PostgREST
    #[arg(long, env = "PIXAMIND_API_CORS", default_value = "true", action = ArgAction::Set, value_parser = BoolishValueParser::new())]
    pub cors: bool,

    /// gzip responses for clients that accept it
    #[arg(long, env = "PIXAMIND_API_COMPRESSION", default_value = "false", action = ArgAction::Set, value_parser = BoolishValueParser::new())]
    pub compression: bool,

    /// How often the function list is re-read from the database
    #[arg(long, env = "PIXAMIND_API_SCHEMA_REFRESH", default_value = "60s", value_parser = parse_duration)]
    pub schema_refresh: Duration,

    /// Log filter, e.g. "info" or "pixamind_api=debug"
    #[arg(long, env = "PIXAMIND_API_LOG", default_value = "info")]
    pub log: String,

    /// Log format
    #[arg(long, env = "PIXAMIND_API_LOG_FORMAT", value_enum, default_value_t = LogFormat::Text)]
    pub log_format: LogFormat,

    /// Ask the running server's /ready (admin listener) and exit 0 if ready, 1 if not; for container health checks
    #[arg(long)]
    pub healthcheck: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
pub enum LogFormat {
    Text,
    Json,
}

/// A listener address that can be turned off.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Listen(pub Option<SocketAddr>);

impl Config {
    pub fn database_url(&self) -> String {
        self.database_url
            .clone()
            .or_else(|| std::env::var("POSTGRES_URL").ok())
            .filter(|url| !url.is_empty())
            .unwrap_or_else(|| DEFAULT_DATABASE_URL.to_string())
    }
}

fn parse_listen(value: &str) -> Result<Listen, String> {
    match value.trim() {
        "" | "off" | "none" | "false" => Ok(Listen(None)),
        addr => addr.parse().map(|a| Listen(Some(a))).map_err(|e| format!("{e}")),
    }
}

/// "250ms", "10s", "2m", "1h"; a bare number is seconds.
pub fn parse_duration(value: &str) -> Result<Duration, String> {
    let value = value.trim();
    let split = value.find(|c: char| !c.is_ascii_digit() && c != '.').unwrap_or(value.len());
    let (number, unit) = value.split_at(split);
    let number: f64 = number.parse().map_err(|_| format!("invalid duration {value:?}"))?;
    let seconds = match unit.trim() {
        "" | "s" => number,
        "ms" => number / 1000.0,
        "m" | "min" => number * 60.0,
        "h" => number * 3600.0,
        other => return Err(format!("invalid duration unit {other:?} in {value:?}")),
    };
    if !seconds.is_finite() || seconds < 0.0 {
        return Err(format!("invalid duration {value:?}"));
    }
    Ok(Duration::from_secs_f64(seconds))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn durations() {
        assert_eq!(parse_duration("10s").unwrap(), Duration::from_secs(10));
        assert_eq!(parse_duration("250ms").unwrap(), Duration::from_millis(250));
        assert_eq!(parse_duration("2m").unwrap(), Duration::from_secs(120));
        assert_eq!(parse_duration("1.5").unwrap(), Duration::from_millis(1500));
        assert!(parse_duration("ten").is_err());
        assert!(parse_duration("5d").is_err());
    }

    #[test]
    fn listen_can_be_turned_off() {
        assert_eq!(parse_listen("off").unwrap(), Listen(None));
        assert_eq!(parse_listen("127.0.0.1:3001").unwrap(), Listen(Some("127.0.0.1:3001".parse().unwrap())));
        assert!(parse_listen("nowhere").is_err());
    }

    #[test]
    fn defaults_match_the_postgrest_setup() {
        let config = Config::parse_from(["pixamind-api"]);
        assert_eq!(config.schema, "hivemind_endpoints");
        assert_eq!(config.role, "hivemind");
        assert_eq!(config.root_function, "home");
        assert_eq!(config.pool_size, 20);
        assert!(config.read_only && config.cors && !config.compression);
    }
}
