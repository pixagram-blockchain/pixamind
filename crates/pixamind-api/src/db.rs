//! The connection pool and the settings every connection gets.

use deadpool_postgres::{Hook, HookError, Manager, ManagerConfig, Pool, PoolError, RecyclingMethod, Runtime};
use tokio_postgres::NoTls;

use crate::config::Config;
use crate::error::ApiError;
use crate::rpc::quote_ident;

/// Not "hivemind_*": the indexer terminates backends with those names.
pub const APPLICATION_NAME: &str = "pixamind-api";

pub fn create_pool(config: &Config) -> anyhow::Result<Pool> {
    let mut pg: tokio_postgres::Config = config.database_url().parse()?;
    pg.application_name(APPLICATION_NAME);
    let manager = Manager::from_config(pg, NoTls, ManagerConfig { recycling_method: RecyclingMethod::Fast });
    let setup = session_setup(config);
    let pool = Pool::builder(manager)
        .max_size(config.pool_size.max(1))
        .runtime(Runtime::Tokio1)
        .wait_timeout(Some(config.pool_timeout))
        .create_timeout(Some(config.pool_timeout))
        .post_create(Hook::async_fn(move |client, _| {
            let setup = setup.clone();
            Box::pin(async move { client.batch_execute(&setup).await.map_err(HookError::Backend) })
        }))
        .build()?;
    Ok(pool)
}

/// What PostgREST sets per request, set once per connection: the API has a
/// single role and never changes it.
pub fn session_setup(config: &Config) -> String {
    let mut sql = Vec::new();
    if !config.role.is_empty() {
        sql.push(format!("SET ROLE {}", quote_ident(&config.role)));
    }
    sql.push(format!("SET search_path TO {}, public", quote_ident(&config.schema)));
    if config.read_only {
        sql.push("SET default_transaction_read_only = on".into());
    }
    if let Some(timeout) = config.statement_timeout {
        sql.push(format!("SET statement_timeout = {}", timeout.as_millis()));
    }
    sql.join("; ")
}

pub fn pool_error(e: PoolError) -> ApiError {
    match e {
        PoolError::Timeout(_) => ApiError::pool_timeout(),
        PoolError::Backend(e) => ApiError::unavailable(e.to_string()),
        PoolError::PostCreateHook(e) => ApiError::unavailable(e.to_string()),
        PoolError::Closed | PoolError::NoRuntimeSpecified => ApiError::unavailable("the connection pool is closed"),
    }
}

pub fn query_error(e: &tokio_postgres::Error) -> ApiError {
    match e.as_db_error() {
        Some(db) => ApiError::from_db(db),
        None => ApiError::unavailable(e.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::Parser;

    #[test]
    fn session_settings() {
        let config = Config::parse_from(["pixamind-api", "--statement-timeout", "30s"]);
        assert_eq!(
            session_setup(&config),
            r#"SET ROLE "hivemind"; SET search_path TO "hivemind_endpoints", public; SET default_transaction_read_only = on; SET statement_timeout = 30000"#
        );
        let config = Config::parse_from(["pixamind-api", "--role", "", "--read-only", "false"]);
        assert_eq!(session_setup(&config), r#"SET search_path TO "hivemind_endpoints", public"#);
    }
}
