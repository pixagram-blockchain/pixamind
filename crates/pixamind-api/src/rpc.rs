//! Calling a function the way PostgREST's `/rpc/<name>` does: pick the overload
//! the arguments fit, pass them through `json_to_record` so PostgreSQL converts
//! them to the parameter types, and shape the result as PostgREST does (scalars
//! as they are, a row as an object, sets as arrays). The SQL mirrors PostgREST
//! 12's, so the response bytes match.

use std::collections::{BTreeMap, BTreeSet};

use axum::http::{HeaderName, HeaderValue, StatusCode};
use bytes::Bytes;
use serde::Deserialize;
use serde::de::IgnoredAny;
use serde_json::{Map, Value};

use crate::error::ApiError;
use crate::schema::{Function, Returns};

/// The arguments a request carries.
pub enum Input {
    /// GET and HEAD: the rewrite rule's arguments, then the query string's.
    Query(Vec<(String, String)>),
    /// POST: the body, plus the rewrite rule's arguments.
    Body { body: Bytes, rule_args: Vec<(String, String)> },
}

enum Payload {
    /// No arguments.
    None,
    /// The raw body, for a function with one unnamed json parameter.
    Body(String),
    /// Named arguments, as a JSON object.
    Named { names: Vec<String>, object: String },
}

/// A call ready to run.
pub struct Call {
    pub sql: String,
    pub argument: Option<String>,
    /// The JSON-RPC method, when the body is a JSON-RPC request.
    pub rpc_method: Option<String>,
}

#[derive(Deserialize)]
struct Probe {
    #[serde(default)]
    method: Option<String>,
}

/// Choose the function `name` and build its call from the request.
pub fn prepare(schema: &str, name: &str, candidates: &[Function], input: Input) -> Result<Call, ApiError> {
    let (function, payload, rpc_method) = match input {
        Input::Query(pairs) => {
            let args = first_wins(pairs.into_iter().map(|(k, v)| (k, Value::String(v))));
            let (function, payload) = resolve_named(schema, name, candidates, args)?;
            (function, payload, None)
        }
        Input::Body { body, rule_args } => resolve_body(schema, name, candidates, &body, rule_args)?,
    };
    let (sql, argument) = build(function, payload);
    Ok(Call { sql, argument, rpc_method })
}

fn first_wins(pairs: impl IntoIterator<Item = (String, Value)>) -> Map<String, Value> {
    let mut map = Map::new();
    for (k, v) in pairs {
        map.entry(k).or_insert(v);
    }
    map
}

fn resolve_named<'a>(
    schema: &str,
    name: &str,
    candidates: &'a [Function],
    args: Map<String, Value>,
) -> Result<(&'a Function, Payload), ApiError> {
    let keys: Vec<&str> = args.keys().map(String::as_str).collect();
    let matching: Vec<&Function> = candidates.iter().filter(|f| f.accepts(&keys)).collect();
    match matching.as_slice() {
        [function] => {
            if args.is_empty() {
                return Ok((function, Payload::None));
            }
            // Function order, so the SQL text (and the prepared statement) is the
            // same whatever order the client sent the arguments in.
            let names: Vec<String> =
                function.params.iter().filter(|p| args.contains_key(&p.name)).map(|p| p.name.clone()).collect();
            let object = Value::Object(args).to_string();
            Ok((function, Payload::Named { names, object }))
        }
        [] => Err(not_found(schema, name, &keys, candidates)),
        _ => Err(ApiError::new(
            StatusCode::MULTIPLE_CHOICES,
            "PGRST203",
            format!(
                "Could not choose the best candidate function between: {}",
                matching.iter().map(|f| f.signature()).collect::<Vec<_>>().join(", ")
            ),
        )),
    }
}

fn resolve_body<'a>(
    schema: &str,
    name: &str,
    candidates: &'a [Function],
    body: &Bytes,
    rule_args: Vec<(String, String)>,
) -> Result<(&'a Function, Payload, Option<String>), ApiError> {
    let empty = body.iter().all(u8::is_ascii_whitespace);
    // Most calls are JSON-RPC to home(json): check the body is JSON and read its
    // method in one pass, without building the request as a tree.
    let probe = if empty { None } else { serde_json::from_slice::<Probe>(body).ok() };
    if !empty && probe.is_none() && serde_json::from_slice::<IgnoredAny>(body).is_err() {
        return Err(ApiError::invalid_json());
    }

    // Named arguments: match on the body's keys first (its values skipped), and
    // only parse the values when a function takes those names.
    let body_keys = if empty {
        Some(Vec::new())
    } else {
        serde_json::from_slice::<BTreeMap<String, IgnoredAny>>(body).ok().map(|m| m.into_keys().collect::<Vec<_>>())
    };
    if let Some(body_keys) = body_keys {
        let mut keys: Vec<&str> =
            rule_args.iter().map(|(k, _)| k.as_str()).chain(body_keys.iter().map(String::as_str)).collect();
        keys.sort_unstable();
        keys.dedup();
        if candidates.iter().any(|f| f.accepts(&keys)) || !candidates.iter().any(Function::takes_body) {
            let object =
                if empty { Map::new() } else { serde_json::from_slice(body).map_err(|_| ApiError::invalid_json())? };
            let args = first_wins(rule_args.into_iter().map(|(k, v)| (k, Value::String(v))).chain(object));
            let (function, payload) = resolve_named(schema, name, candidates, args)?;
            return Ok((function, payload, None));
        }
    }

    match candidates.iter().find(|f| f.takes_body()) {
        Some(_) if empty => Err(ApiError::invalid_json()),
        Some(function) => {
            let text = std::str::from_utf8(body).map_err(|_| ApiError::invalid_json())?;
            Ok((function, Payload::Body(text.to_string()), probe.and_then(|p| p.method)))
        }
        None => Err(not_found(schema, name, &[], candidates)),
    }
}

fn not_found(schema: &str, name: &str, keys: &[&str], candidates: &[Function]) -> ApiError {
    let sorted: BTreeSet<&str> = keys.iter().copied().collect();
    let listed = sorted.into_iter().collect::<Vec<_>>().join(", ");
    let (message, details) = if keys.is_empty() {
        (
            format!("Could not find the function {schema}.{name} without parameters in the schema cache"),
            format!(
                "Searched for the function {schema}.{name} without parameters or with a single unnamed json/jsonb parameter, but no matches were found in the schema cache."
            ),
        )
    } else {
        (
            format!("Could not find the function {schema}.{name}({listed}) in the schema cache"),
            format!(
                "Searched for the function {schema}.{name} with parameters {listed} or with a single unnamed json/jsonb parameter, but no matches were found in the schema cache."
            ),
        )
    };
    let error = ApiError::new(StatusCode::NOT_FOUND, "PGRST202", message).details(details);
    match candidates.first() {
        Some(f) => error.hint(format!("Perhaps you meant to call the function {}", f.signature())),
        None => error,
    }
}

pub fn quote_ident(name: &str) -> String {
    format!("\"{}\"", name.replace('"', "\"\""))
}

/// The statement and its single text argument.
fn build(f: &Function, payload: Payload) -> (String, Option<String>) {
    let function = format!("{}.{}", quote_ident(&f.schema), quote_ident(&f.name));
    let (args_from, call, argument) = match payload {
        Payload::None => (String::new(), format!("{function}()"), None),
        Payload::Body(body) => (String::new(), format!("{function}($1::text::{})", f.params[0].pg_type), Some(body)),
        Payload::Named { names, object } => {
            let typed = |n: &String| f.param(n).map_or("text", |p| p.pg_type.as_str()).to_string();
            let columns = names.iter().map(|n| quote_ident(n)).collect::<Vec<_>>().join(", ");
            let definitions =
                names.iter().map(|n| format!("{} {}", quote_ident(n), typed(n))).collect::<Vec<_>>().join(", ");
            let named =
                names.iter().map(|n| format!("{0} := pgrst_args.{0}", quote_ident(n))).collect::<Vec<_>>().join(", ");
            (
                format!(
                    "(SELECT $1::text::json AS json_data) pgrst_payload, LATERAL (SELECT {columns} FROM json_to_record(pgrst_payload.json_data) AS _({definitions}) LIMIT 1) pgrst_args, "
                ),
                format!("{function}({named})"),
                Some(object),
            )
        }
    };
    let (source, body) = match f.returns {
        Returns::Scalar => (
            format!(
                "SELECT pgrst_call.pgrst_scalar FROM {args_from}LATERAL (SELECT {call} AS pgrst_scalar) pgrst_call"
            ),
            "coalesce((json_agg(_postgrest_t.pgrst_scalar)->0)::text, 'null')",
        ),
        Returns::SetOfScalar => (
            format!("SELECT pgrst_call.pgrst_scalar FROM {args_from}LATERAL {call} AS pgrst_call(pgrst_scalar)"),
            "coalesce(json_agg(_postgrest_t.pgrst_scalar)::text, '[]')",
        ),
        Returns::Composite => (
            format!("SELECT pgrst_call.* FROM {args_from}LATERAL {call} pgrst_call"),
            "coalesce((json_agg(_postgrest_t)->0)::text, 'null')",
        ),
        Returns::SetOfComposite => (
            format!("SELECT pgrst_call.* FROM {args_from}LATERAL {call} pgrst_call"),
            "coalesce(json_agg(_postgrest_t)::text, '[]')",
        ),
    };
    // count(*), not count(_postgrest_t): a whole-row reference makes the planner copy a
    // scalar call into ROW(...) and run the function twice.
    let sql = format!(
        "WITH pgrst_source AS ({source}) \
         SELECT {body} AS body, \
                pg_catalog.count(*) AS page_total, \
                nullif(current_setting('response.headers', true), '') AS response_headers, \
                nullif(current_setting('response.status', true), '') AS response_status \
         FROM (SELECT * FROM pgrst_source) _postgrest_t"
    );
    (sql, argument)
}

/// What the function answered.
#[derive(Debug)]
pub struct Answer {
    pub status: StatusCode,
    /// PostgREST's `Content-Range` for the returned rows: "0-0/*" for one value.
    pub content_range: String,
    pub headers: Vec<(HeaderName, HeaderValue)>,
    pub body: String,
}

/// Turn the statement's row into a response, applying `response.headers` and
/// `response.status` as PostgREST does.
pub fn answer(
    body: Option<String>,
    rows: i64,
    headers: Option<String>,
    status: Option<String>,
) -> Result<Answer, ApiError> {
    let status = match status {
        None => StatusCode::OK,
        Some(s) => s.trim().parse::<u16>().ok().and_then(|n| StatusCode::from_u16(n).ok()).ok_or_else(|| {
            ApiError::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                "PGRST112",
                "response.status guc must be a valid status code",
            )
        })?,
    };
    let headers = match headers {
        None => Vec::new(),
        Some(text) => parse_headers(&text).ok_or_else(|| {
            ApiError::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                "PGRST111",
                "response.headers guc must be a JSON array composed of objects with a single key and a string value",
            )
        })?,
    };
    let content_range = if rows > 0 { format!("0-{}/*", rows - 1) } else { "*/*".to_string() };
    Ok(Answer { status, content_range, headers, body: body.unwrap_or_else(|| "null".into()) })
}

fn parse_headers(text: &str) -> Option<Vec<(HeaderName, HeaderValue)>> {
    let list: Vec<Map<String, Value>> = serde_json::from_str(text).ok()?;
    list.into_iter()
        .map(|object| {
            let mut entries = object.into_iter();
            let (name, value) = entries.next()?;
            if entries.next().is_some() {
                return None;
            }
            let value = value.as_str()?;
            Some((HeaderName::from_bytes(name.as_bytes()).ok()?, HeaderValue::from_str(value).ok()?))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::function;

    const S: &str = "hivemind_endpoints";

    fn home() -> Vec<Function> {
        vec![function("home", &[("", "json")], 0, Returns::Scalar)]
    }

    fn body(text: &str) -> Input {
        Input::Body { body: Bytes::from(text.to_string()), rule_args: vec![] }
    }

    #[test]
    fn json_rpc_goes_to_home_with_the_raw_body() {
        let candidates = home();
        let request = r#"{"jsonrpc":"2.0","id":1,"method":"bridge.get_post","params":{"author":"a","permlink":"p"}}"#;
        let call = prepare(S, "home", &candidates, body(request)).unwrap();
        assert_eq!(call.argument.as_deref(), Some(request));
        assert_eq!(call.rpc_method.as_deref(), Some("bridge.get_post"));
        assert!(call.sql.contains(r#""hivemind_endpoints"."home"($1::text::json) AS pgrst_scalar"#), "{}", call.sql);
        assert!(call.sql.contains("json_agg(_postgrest_t.pgrst_scalar)->0"));
        assert!(!call.sql.contains("count(_postgrest_t)"), "the function would run twice");
    }

    #[test]
    fn home_overloads() {
        // home(json) answers JSON-RPC; home() returns the OpenAPI document (PostgREST's root spec).
        let candidates =
            vec![function("home", &[("", "json")], 0, Returns::Scalar), function("home", &[], 0, Returns::Scalar)];
        let call = prepare(S, "home", &candidates, body(r#"{"jsonrpc":"2.0","id":1,"method":"m.n"}"#)).unwrap();
        assert!(call.sql.contains("$1::text::json) AS pgrst_scalar"));
        for input in [Input::Query(vec![]), body("{}"), body("")] {
            let call = prepare(S, "home", &candidates, input).unwrap();
            assert!(call.argument.is_none());
            assert!(call.sql.contains(r#""hivemind_endpoints"."home"() AS pgrst_scalar"#), "{}", call.sql);
        }
    }

    #[test]
    fn invalid_or_empty_json_is_rejected_before_the_database() {
        let candidates = home();
        assert_eq!(prepare(S, "home", &candidates, body("{nope")).err().unwrap().code, "PGRST102");
        assert_eq!(prepare(S, "home", &candidates, body("  ")).err().unwrap().code, "PGRST102");
    }

    #[test]
    fn a_json_array_still_reaches_home() {
        // home() answers it with its own error, as behind PostgREST.
        let candidates = home();
        let call = prepare(S, "home", &candidates, body(r#"[{"jsonrpc":"2.0"}]"#)).unwrap();
        assert_eq!(call.rpc_method, None);
    }

    #[test]
    fn query_arguments_are_named_and_typed() {
        let candidates = vec![function(
            "get_ops_by_account",
            &[("account-name", "text"), ("page", "integer"), ("page-size", "integer")],
            2,
            Returns::Composite,
        )];
        let input = Input::Query(vec![
            ("page-size".into(), "20".into()),
            ("account-name".into(), "alice".into()),
            ("account-name".into(), "mallory".into()),
        ]);
        let call = prepare(S, "get_ops_by_account", &candidates, input).unwrap();
        // first value wins: the rule's argument comes before the query string's
        assert_eq!(call.argument.as_deref(), Some(r#"{"account-name":"alice","page-size":"20"}"#));
        assert!(
            call.sql
                .contains(r#"json_to_record(pgrst_payload.json_data) AS _("account-name" text, "page-size" integer)"#),
            "{}",
            call.sql
        );
        assert!(
            call.sql.contains(r#""account-name" := pgrst_args."account-name", "page-size" := pgrst_args."page-size""#)
        );
        assert!(call.sql.contains("(json_agg(_postgrest_t)->0)"));
    }

    #[test]
    fn unknown_or_missing_arguments_are_404() {
        let candidates =
            vec![function("get_reblogs", &[("sort", "text"), ("limit", "integer")], 1, Returns::SetOfComposite)];
        let e = prepare(S, "get_reblogs", &candidates, Input::Query(vec![("bogus".into(), "1".into())])).err().unwrap();
        assert_eq!((e.status.as_u16(), e.code.as_str()), (404, "PGRST202"));
        assert_eq!(e.message, "Could not find the function hivemind_endpoints.get_reblogs(bogus) in the schema cache");
        assert_eq!(
            e.hint.as_deref(),
            Some("Perhaps you meant to call the function hivemind_endpoints.get_reblogs(limit, sort)")
        );
        let e = prepare(S, "get_reblogs", &candidates, Input::Query(vec![])).err().unwrap();
        assert_eq!(e.code, "PGRST202");
    }

    #[test]
    fn post_body_arguments_merge_with_the_rule_arguments() {
        let candidates =
            vec![function("get_account_pending_author_rewards", &[("account-name", "text")], 0, Returns::Composite)];
        let input = Input::Body {
            body: Bytes::from_static(br#"{"account-name": "body"}"#),
            rule_args: vec![("account-name".into(), "path".into())],
        };
        let call = prepare(S, "get_account_pending_author_rewards", &candidates, input).unwrap();
        assert_eq!(call.argument.as_deref(), Some(r#"{"account-name":"path"}"#));
    }

    #[test]
    fn no_arguments_and_sets() {
        let candidates = vec![function("get_sync_status", &[], 0, Returns::Scalar)];
        let call = prepare(S, "get_sync_status", &candidates, Input::Query(vec![])).unwrap();
        assert!(call.argument.is_none());
        assert!(
            call.sql.contains(r#"FROM LATERAL (SELECT "hivemind_endpoints"."get_sync_status"() AS pgrst_scalar)"#),
            "{}",
            call.sql
        );

        let candidates = vec![function("ids", &[], 0, Returns::SetOfScalar)];
        let call = prepare(S, "ids", &candidates, Input::Query(vec![])).unwrap();
        assert!(call.sql.contains("AS pgrst_call(pgrst_scalar)"));
        assert!(call.sql.contains("coalesce(json_agg(_postgrest_t.pgrst_scalar)::text, '[]')"));
    }

    #[test]
    fn empty_post_body_calls_without_arguments() {
        let candidates = vec![function("get_sync_status", &[], 0, Returns::Scalar)];
        assert!(prepare(S, "get_sync_status", &candidates, body("")).unwrap().argument.is_none());
    }

    #[test]
    fn response_settings() {
        let a = answer(Some("{}".into()), 1, Some(r#"[{"Cache-Control": "public, max-age=2"}]"#.into()), None).unwrap();
        assert_eq!(a.status, StatusCode::OK);
        assert_eq!(a.headers[0].0, "cache-control");
        assert_eq!(a.headers[0].1, "public, max-age=2");
        assert_eq!(a.content_range, "0-0/*");
        assert_eq!(answer(Some("[]".into()), 0, None, None).unwrap().content_range, "*/*");
        assert_eq!(answer(Some("[1, 2]".into()), 2, None, None).unwrap().content_range, "0-1/*");
        assert_eq!(answer(None, 1, None, Some("201".into())).unwrap().status.as_u16(), 201);
        assert_eq!(answer(None, 1, Some("{}".into()), None).err().unwrap().code, "PGRST111");
        assert_eq!(answer(None, 1, Some(r#"[{"a":"1","b":"2"}]"#.into()), None).err().unwrap().code, "PGRST111");
        assert_eq!(answer(None, 1, None, Some("abc".into())).err().unwrap().code, "PGRST112");
    }

    #[test]
    fn identifiers_are_quoted() {
        assert_eq!(quote_ident(r#"we"ird"#), r#""we""ird""#);
    }
}
