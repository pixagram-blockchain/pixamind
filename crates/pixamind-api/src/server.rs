//! The HTTP side: routing a request to a function, running it, and the admin
//! endpoints.

use std::borrow::Cow;
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};

use axum::Router;
use axum::body::Body;
use axum::extract::rejection::BytesRejection;
use axum::extract::{DefaultBodyLimit, Request, State};
use axum::http::{HeaderValue, Method, StatusCode, Uri, header};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use bytes::Bytes;
use deadpool_postgres::Pool;
use percent_encoding::percent_decode_str;
use tokio_postgres::NoTls;
use tower_http::compression::CompressionLayer;

use crate::config::Config;
use crate::db::{pool_error, query_error};
use crate::error::{ApiError, JSON_UTF8};
use crate::metrics::Metrics;
use crate::rewrite::RewriteRules;
use crate::rpc::{self, Input};
use crate::schema::{self, SchemaCache};

const SERVER: &str = concat!("pixamind-api/", env!("CARGO_PKG_VERSION"));

/// A function missing from the cache triggers a reload, at most this often
/// (requests for unknown names must not turn into a catalog query each).
const RELOAD_ON_MISS_INTERVAL: Duration = Duration::from_secs(2);

pub struct AppState {
    pub config: Config,
    pub rules: RewriteRules,
    pub pool: Pool,
    pub metrics: Metrics,
    schema: RwLock<Arc<SchemaCache>>,
    last_reload: tokio::sync::Mutex<Option<Instant>>,
}

impl AppState {
    pub fn new(config: Config, rules: RewriteRules, pool: Pool) -> Self {
        Self {
            config,
            rules,
            pool,
            metrics: Metrics::new(),
            schema: RwLock::new(Arc::new(SchemaCache::default())),
            last_reload: tokio::sync::Mutex::new(None),
        }
    }

    pub fn schema(&self) -> Arc<SchemaCache> {
        self.schema.read().unwrap_or_else(|e| e.into_inner()).clone()
    }

    /// Re-read the function list, unless it was read less than `min_interval` ago.
    pub async fn reload_schema(&self, min_interval: Duration) -> Result<(), ApiError> {
        let mut last = self.last_reload.lock().await;
        if let Some(at) = *last
            && at.elapsed() < min_interval
        {
            return Ok(());
        }
        *last = Some(Instant::now());
        let client = self.pool.get().await.map_err(pool_error)?;
        let cache = schema::load(&client, &self.config.schema)
            .await
            .map_err(|e| ApiError::schema_cache_unavailable(e.to_string()))?;
        if cache.count() == 0 {
            tracing::warn!(schema = %self.config.schema, "no functions found in the schema");
        }
        self.metrics.set_functions(cache.count());
        *self.schema.write().unwrap_or_else(|e| e.into_inner()) = Arc::new(cache);
        Ok(())
    }
}

pub fn api_router(state: Arc<AppState>) -> Router {
    let mut router =
        Router::new().fallback(api).layer(DefaultBodyLimit::max(state.config.max_body_bytes)).with_state(state.clone());
    if state.config.compression {
        router = router.layer(CompressionLayer::new());
    }
    if state.config.cors {
        router = router.layer(middleware::from_fn(cors));
    }
    router
}

/// PostgREST's CORS policy, header for header: any origin, the requested
/// headers, a day of caching. Requests without an Origin get no CORS headers.
async fn cors(request: Request, next: Next) -> Response {
    const EXPOSED: &str = "Content-Encoding, Content-Location, Content-Range, Content-Type, Date, Location, Server, Transfer-Encoding, Range-Unit";
    let headers = request.headers();
    let has_origin = headers.contains_key(header::ORIGIN);
    if has_origin && request.method() == Method::OPTIONS && headers.contains_key(header::ACCESS_CONTROL_REQUEST_METHOD)
    {
        let requested = headers.get(header::ACCESS_CONTROL_REQUEST_HEADERS).and_then(|v| v.to_str().ok()).unwrap_or("");
        let mut allowed: Vec<&str> = vec!["Authorization"];
        let simple = ["Accept", "Accept-Language", "Content-Language", "Content-Type"];
        for name in requested.split(',').map(str::trim).filter(|h| !h.is_empty()).chain(simple) {
            if !allowed.iter().any(|a| a.eq_ignore_ascii_case(name)) {
                allowed.push(name);
            }
        }
        let mut response = StatusCode::OK.into_response();
        let out = response.headers_mut();
        out.insert(header::ACCESS_CONTROL_ALLOW_ORIGIN, HeaderValue::from_static("*"));
        out.insert(
            header::ACCESS_CONTROL_ALLOW_METHODS,
            HeaderValue::from_static("GET, POST, PATCH, PUT, DELETE, OPTIONS, HEAD"),
        );
        if let Ok(value) = HeaderValue::from_str(&allowed.join(", ")) {
            out.insert(header::ACCESS_CONTROL_ALLOW_HEADERS, value);
        }
        out.insert(header::ACCESS_CONTROL_MAX_AGE, HeaderValue::from_static("86400"));
        return response;
    }
    let mut response = next.run(request).await;
    if has_origin {
        let out = response.headers_mut();
        out.insert(header::ACCESS_CONTROL_ALLOW_ORIGIN, HeaderValue::from_static("*"));
        out.insert(header::ACCESS_CONTROL_EXPOSE_HEADERS, HeaderValue::from_static(EXPOSED));
    }
    response
}

pub fn admin_router(state: Arc<AppState>) -> Router {
    Router::new()
        .route("/health", get(|| async { StatusCode::NO_CONTENT }))
        .route("/live", get(|| async { StatusCode::OK }))
        .route("/ready", get(ready))
        .route("/metrics", get(metrics))
        .with_state(state)
}

struct InFlight<'a>(&'a prometheus::IntGauge);

impl Drop for InFlight<'_> {
    fn drop(&mut self) {
        self.0.dec();
    }
}

async fn api(
    State(state): State<Arc<AppState>>,
    method: Method,
    uri: Uri,
    body: Result<Bytes, BytesRejection>,
) -> Response {
    let started = Instant::now();
    state.metrics.in_flight.inc();
    let _in_flight = InFlight(&state.metrics.in_flight);

    let mut outcome = Outcome::default();
    let response = match route(&state, &method, &uri, body, &mut outcome).await {
        Ok(response) => response,
        Err(error) => {
            if error.status.is_server_error() {
                tracing::warn!(path = %uri.path(), function = ?outcome.function, method = ?outcome.rpc_method, status = error.status.as_u16(), code = %error.code, "{}", error.message);
            }
            error.into_response()
        }
    };
    let status = response.status().as_u16();
    let function = outcome.function.as_deref().unwrap_or("");
    let rpc_method = state.metrics.method_label(outcome.rpc_method.as_deref());
    state.metrics.observe(function, &rpc_method, status, started.elapsed());
    tracing::debug!(http_method = %method, path = %uri.path(), function, method = rpc_method, status, elapsed_ms = started.elapsed().as_secs_f64() * 1000.0, "request");
    let mut response = response;
    response.headers_mut().insert(header::SERVER, HeaderValue::from_static(SERVER));
    if method == Method::HEAD {
        let (parts, _) = response.into_parts();
        return Response::from_parts(parts, Body::empty());
    }
    response
}

#[derive(Default)]
struct Outcome {
    function: Option<String>,
    rpc_method: Option<String>,
}

async fn route(
    state: &AppState,
    method: &Method,
    uri: &Uri,
    body: Result<Bytes, BytesRejection>,
    outcome: &mut Outcome,
) -> Result<Response, ApiError> {
    let body = body.map_err(|rejection| {
        if rejection.status() == StatusCode::PAYLOAD_TOO_LARGE {
            ApiError::body_too_large(state.config.max_body_bytes)
        } else {
            ApiError::new(StatusCode::BAD_REQUEST, "PXM400", rejection.body_text())
        }
    })?;

    let post = match *method {
        Method::GET | Method::HEAD => false,
        Method::POST => true,
        Method::OPTIONS => {
            let mut response = StatusCode::OK.into_response();
            response.headers_mut().insert(header::ALLOW, HeaderValue::from_static("OPTIONS,GET,HEAD,POST"));
            return Ok(response);
        }
        _ => {
            let message = format!("Cannot use the {method} method on RPC");
            return Err(ApiError::new(StatusCode::METHOD_NOT_ALLOWED, "PGRST101", message));
        }
    };

    let path = normalize_path(uri.path());
    let Some(Target { name, mut args, keep_query }) = target(state, &path) else {
        // What PostgREST answers for a path that is neither a function nor a table.
        let mut response = (StatusCode::NOT_FOUND, "{}").into_response();
        response.headers_mut().insert(header::CONTENT_TYPE, HeaderValue::from_static(JSON_UTF8));
        return Ok(response);
    };
    if keep_query && let Some(query) = uri.query() {
        args.extend(form_urlencoded::parse(query.as_bytes()).map(|(k, v)| (k.into_owned(), v.into_owned())));
    }
    outcome.function = Some(name.clone());

    let mut cache = state.schema();
    if !cache.is_loaded() {
        state.reload_schema(Duration::from_secs(1)).await?;
        cache = state.schema();
    }
    if cache.get(&name).is_none() {
        state.reload_schema(RELOAD_ON_MISS_INTERVAL).await?;
        cache = state.schema();
    }
    let candidates = cache.get(&name).unwrap_or(&[]);
    if candidates.is_empty() {
        outcome.function = None; // unknown names don't become metric labels
    }
    let input = if post { Input::Body { body, rule_args: args } } else { Input::Query(args) };
    let call = rpc::prepare(&state.config.schema, &name, candidates, input)?;
    outcome.rpc_method = call.rpc_method.clone();

    let answer = execute(state, &call).await?;
    let mut response = (answer.status, answer.body).into_response();
    let headers = response.headers_mut();
    headers.insert(header::CONTENT_TYPE, HeaderValue::from_static(JSON_UTF8));
    if let Ok(range) = HeaderValue::from_str(&answer.content_range) {
        headers.insert(header::CONTENT_RANGE, range);
    }
    for (name, value) in answer.headers {
        headers.insert(name, value);
    }
    Ok(response)
}

/// nginx matches the decoded path with duplicate slashes merged.
fn normalize_path(path: &str) -> String {
    let decoded: Cow<'_, str> = percent_decode_str(path).decode_utf8_lossy();
    let mut out = String::with_capacity(decoded.len());
    for c in decoded.chars() {
        if c == '/' && out.ends_with('/') {
            continue;
        }
        out.push(c);
    }
    if out.is_empty() { "/".into() } else { out }
}

/// The function a path calls.
struct Target {
    name: String,
    /// The rewrite rule's arguments.
    args: Vec<(String, String)>,
    /// Whether the query string's arguments are passed too.
    keep_query: bool,
}

fn target(state: &AppState, path: &str) -> Option<Target> {
    let target = |name: String, args, keep_query| Target { name, args, keep_query };
    // PostgREST's own paths, as the tavern suite and internal callers use them.
    if let Some(name) = path.strip_prefix("/rpc/") {
        return function_name(name).map(|n| target(n, Vec::new(), true));
    }
    match state.rules.apply(path) {
        Some(rewritten) if rewritten.path == "/" => {
            Some(target(state.config.root_function.clone(), rewritten.args, rewritten.keep_request_args))
        }
        Some(rewritten) => {
            let name = function_name(rewritten.path.strip_prefix("/rpc/")?)?;
            Some(target(name, rewritten.args, rewritten.keep_request_args))
        }
        None if path == "/" => Some(target(state.config.root_function.clone(), Vec::new(), true)),
        None => None,
    }
}

fn function_name(name: &str) -> Option<String> {
    (!name.is_empty() && !name.contains('/')).then(|| name.to_string())
}

async fn execute(state: &AppState, call: &rpc::Call) -> Result<rpc::Answer, ApiError> {
    let started = Instant::now();
    let mut retried = false;
    loop {
        let client = state.pool.get().await.map_err(pool_error)?;
        let cancel = client.cancel_token();
        let remaining = state.config.request_timeout.saturating_sub(started.elapsed());
        let run = async {
            let statement = client.prepare_cached(&call.sql).await?;
            match &call.argument {
                Some(argument) => client.query_one(&statement, &[argument]).await,
                None => client.query_one(&statement, &[]).await,
            }
        };
        match tokio::time::timeout(remaining, run).await {
            Ok(Ok(row)) => return rpc::answer(row.get(0), row.get(1), row.get(2), row.get(3)),
            Ok(Err(e)) => {
                let error = query_error(&e);
                // A function recreated with another signature since the cache was read.
                if !retried && error.code == "42883" {
                    retried = true;
                    drop(client);
                    state.reload_schema(RELOAD_ON_MISS_INTERVAL).await?;
                    continue;
                }
                return Err(error);
            }
            Err(_) => {
                tokio::spawn(async move {
                    if let Err(e) = cancel.cancel_query(NoTls).await {
                        tracing::warn!("could not cancel a timed out query: {e}");
                    }
                });
                // Don't hand a connection with a cancelled query back to the pool.
                let _ = deadpool_postgres::Object::take(client);
                return Err(ApiError::request_timeout(state.config.request_timeout.as_secs_f64()));
            }
        }
    }
}

async fn ready(State(state): State<Arc<AppState>>) -> StatusCode {
    let check = async {
        let client = state.pool.get().await.ok()?;
        client.simple_query("SELECT 1").await.ok()?;
        drop(client);
        if !state.schema().is_loaded() {
            state.reload_schema(Duration::from_secs(1)).await.ok()?;
        }
        state.schema().is_loaded().then_some(())
    };
    match tokio::time::timeout(Duration::from_secs(5), check).await {
        Ok(Some(())) => StatusCode::OK,
        _ => StatusCode::SERVICE_UNAVAILABLE,
    }
}

async fn metrics(State(state): State<Arc<AppState>>) -> Response {
    let status = state.pool.status();
    state.metrics.set_pool(status.size, status.available, status.waiting);
    let mut response = state.metrics.render().into_response();
    response
        .headers_mut()
        .insert(header::CONTENT_TYPE, HeaderValue::from_static("text/plain; version=0.0.4; charset=utf-8"));
    response
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn paths_are_decoded_and_slashes_merged() {
        assert_eq!(normalize_path("/accounts/al%69ce//operations"), "/accounts/alice/operations");
        assert_eq!(normalize_path(""), "/");
        assert_eq!(normalize_path("//"), "/");
    }

    #[test]
    fn function_names() {
        assert_eq!(function_name("home").as_deref(), Some("home"));
        assert!(function_name("").is_none());
        assert!(function_name("a/b").is_none());
    }
}
