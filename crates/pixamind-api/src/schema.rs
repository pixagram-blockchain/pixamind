//! The functions of the served schema, read from the catalog (PostgREST's schema cache).

use std::collections::HashMap;
use std::time::Instant;

use deadpool_postgres::GenericClient;
use serde::Deserialize;

/// One input parameter of a function.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct Param {
    /// Empty for an unnamed parameter.
    pub name: String,
    /// The type as `format_type()` writes it, usable in SQL.
    #[serde(rename = "type")]
    pub pg_type: String,
}

/// How a function's result becomes the response body.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Returns {
    /// One value, sent as JSON (`json`/`jsonb` results as they are).
    Scalar,
    /// One row, sent as an object.
    Composite,
    /// `SETOF` a base type, sent as an array of values.
    SetOfScalar,
    /// `SETOF` rows or `RETURNS TABLE`, sent as an array of objects.
    SetOfComposite,
}

#[derive(Debug, Clone)]
pub struct Function {
    pub schema: String,
    pub name: String,
    pub params: Vec<Param>,
    /// Parameters without a default (they come first).
    pub required: usize,
    pub returns: Returns,
}

impl Function {
    /// A function taking the whole request body, like `home(json)`.
    pub fn takes_body(&self) -> bool {
        self.params.len() == 1
            && self.params[0].name.is_empty()
            && matches!(self.params[0].pg_type.as_str(), "json" | "jsonb")
    }

    pub fn param(&self, name: &str) -> Option<&Param> {
        self.params.iter().find(|p| !p.name.is_empty() && p.name == name)
    }

    /// Whether these argument names can call this function.
    pub fn accepts(&self, keys: &[&str]) -> bool {
        if self.params.iter().any(|p| p.name.is_empty()) {
            return false; // unnamed parameters can't be passed by name
        }
        keys.iter().all(|k| self.param(k).is_some())
            && self.params[..self.required].iter().all(|p| keys.contains(&p.name.as_str()))
    }

    /// "name(a, b)" for messages, parameters sorted like PostgREST does.
    pub fn signature(&self) -> String {
        let mut names: Vec<&str> = self.params.iter().map(|p| p.name.as_str()).collect();
        names.sort_unstable();
        format!("{}.{}({})", self.schema, self.name, names.join(", "))
    }
}

#[derive(Debug, Default)]
pub struct SchemaCache {
    pub functions: HashMap<String, Vec<Function>>,
    pub loaded_at: Option<Instant>,
}

impl SchemaCache {
    pub fn is_loaded(&self) -> bool {
        self.loaded_at.is_some()
    }

    pub fn count(&self) -> usize {
        self.functions.values().map(Vec::len).sum()
    }

    pub fn get(&self, name: &str) -> Option<&[Function]> {
        self.functions.get(name).map(Vec::as_slice)
    }
}

const INTROSPECT: &str = r#"
WITH f AS (
  SELECT p.proname, p.pronargdefaults, p.proretset, p.prorettype,
         coalesce(p.proallargtypes, p.proargtypes::oid[]) AS types,
         coalesce(p.proargmodes, array_fill('i'::"char", ARRAY[p.pronargs::int])) AS modes,
         coalesce(p.proargnames, array_fill(''::text, ARRAY[coalesce(array_length(p.proallargtypes, 1), p.pronargs::int)])) AS names
  FROM pg_catalog.pg_proc p
  JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
  WHERE n.nspname = $1 AND p.prokind = 'f'
)
SELECT f.proname::text,
       f.pronargdefaults::int,
       f.proretset,
       (t.typtype = 'c' OR (t.typtype = 'p' AND t.typname = 'record')) AS composite,
       coalesce((SELECT json_agg(json_build_object('name', coalesce(a.name, ''), 'type', format_type(a.type, NULL)) ORDER BY a.ord)
                   FROM unnest(f.types, f.modes, f.names) WITH ORDINALITY AS a(type, mode, name, ord)
                  WHERE a.mode IN ('i', 'b', 'v')), '[]')::text AS params
FROM f
JOIN pg_catalog.pg_type t ON t.oid = f.prorettype
ORDER BY 1
"#;

pub async fn load(client: &impl GenericClient, schema: &str) -> Result<SchemaCache, LoadError> {
    let rows = client.query(INTROSPECT, &[&schema]).await?;
    let mut functions: HashMap<String, Vec<Function>> = HashMap::new();
    for row in rows {
        let name: String = row.get(0);
        let defaults: i32 = row.get(1);
        let set: bool = row.get(2);
        let composite: bool = row.get(3);
        let params: Vec<Param> = serde_json::from_str(row.get::<_, &str>(4))?;
        let required = params.len().saturating_sub(defaults.max(0) as usize);
        let returns = match (set, composite) {
            (false, false) => Returns::Scalar,
            (false, true) => Returns::Composite,
            (true, false) => Returns::SetOfScalar,
            (true, true) => Returns::SetOfComposite,
        };
        functions.entry(name.clone()).or_default().push(Function {
            schema: schema.to_string(),
            name,
            params,
            required,
            returns,
        });
    }
    Ok(SchemaCache { functions, loaded_at: Some(Instant::now()) })
}

#[derive(Debug)]
pub enum LoadError {
    Db(tokio_postgres::Error),
    Json(serde_json::Error),
}

impl From<tokio_postgres::Error> for LoadError {
    fn from(e: tokio_postgres::Error) -> Self {
        Self::Db(e)
    }
}

impl From<serde_json::Error> for LoadError {
    fn from(e: serde_json::Error) -> Self {
        Self::Json(e)
    }
}

impl std::fmt::Display for LoadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Db(e) => write!(f, "{e}"),
            Self::Json(e) => write!(f, "unexpected catalog data: {e}"),
        }
    }
}

#[cfg(test)]
pub(crate) fn function(name: &str, params: &[(&str, &str)], defaults: usize, returns: Returns) -> Function {
    Function {
        schema: "hivemind_endpoints".into(),
        name: name.into(),
        params: params.iter().map(|(n, t)| Param { name: (*n).into(), pg_type: (*t).into() }).collect(),
        required: params.len() - defaults,
        returns,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn named_arguments_must_cover_required_parameters() {
        let f = function(
            "get_ops_by_account",
            &[("account-name", "text"), ("page", "integer"), ("page-size", "integer")],
            2,
            Returns::Composite,
        );
        assert!(f.accepts(&["account-name"]));
        assert!(f.accepts(&["page-size", "account-name"]));
        assert!(!f.accepts(&["page"]));
        assert!(!f.accepts(&["account-name", "nope"]));
        assert!(!f.takes_body());
    }

    #[test]
    fn home_takes_the_body() {
        let home = function("home", &[("", "json")], 0, Returns::Scalar);
        assert!(home.takes_body());
        assert!(!home.accepts(&[]));
    }

    #[test]
    fn signature_lists_sorted_parameters() {
        let f = function("get_reblogs", &[("sort", "text"), ("limit", "integer")], 1, Returns::SetOfComposite);
        assert_eq!(f.signature(), "hivemind_endpoints.get_reblogs(limit, sort)");
    }
}
