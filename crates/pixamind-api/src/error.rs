//! Errors in PostgREST's shape: `{"code","details","hint","message"}` with the
//! HTTP status PostgREST derives from the SQLSTATE.

use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use serde_json::json;
use tokio_postgres::error::DbError;

pub const JSON_UTF8: &str = "application/json; charset=utf-8";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ApiError {
    pub status: StatusCode,
    pub code: String,
    pub message: String,
    pub details: Option<String>,
    pub hint: Option<String>,
}

impl ApiError {
    pub fn new(status: StatusCode, code: &str, message: impl Into<String>) -> Self {
        Self { status, code: code.into(), message: message.into(), details: None, hint: None }
    }

    pub fn details(mut self, details: impl Into<String>) -> Self {
        self.details = Some(details.into());
        self
    }

    pub fn hint(mut self, hint: impl Into<String>) -> Self {
        self.hint = Some(hint.into());
        self
    }

    /// An error raised by the database.
    pub fn from_db(e: &DbError) -> Self {
        let code = e.code().code();
        Self {
            status: status_for_sqlstate(code),
            code: code.to_string(),
            message: e.message().to_string(),
            details: e.detail().map(str::to_string),
            hint: e.hint().map(str::to_string),
        }
    }

    pub fn invalid_json() -> Self {
        Self::new(StatusCode::BAD_REQUEST, "PGRST102", "Empty or invalid json")
    }

    pub fn body_too_large(limit: usize) -> Self {
        Self::new(StatusCode::PAYLOAD_TOO_LARGE, "PXM413", format!("The request body is larger than {limit} bytes"))
    }

    pub fn pool_timeout() -> Self {
        Self::new(StatusCode::GATEWAY_TIMEOUT, "PGRST003", "Timed out acquiring connection from connection pool.")
    }

    pub fn unavailable(details: impl Into<String>) -> Self {
        Self::new(StatusCode::SERVICE_UNAVAILABLE, "PGRST001", "Database client error. Retrying the connection.")
            .details(details)
    }

    pub fn schema_cache_unavailable(details: impl Into<String>) -> Self {
        Self::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "PGRST002",
            "Could not query the database for the schema cache. Retrying.",
        )
        .details(details)
    }

    pub fn request_timeout(seconds: f64) -> Self {
        Self::new(StatusCode::GATEWAY_TIMEOUT, "PXM504", format!("The request was cancelled after {seconds}s"))
    }

    pub fn body(&self) -> String {
        json!({
            "code": self.code,
            "details": self.details,
            "hint": self.hint,
            "message": self.message,
        })
        .to_string()
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let mut response = (self.status, self.body()).into_response();
        response.headers_mut().insert(header::CONTENT_TYPE, HeaderValue::from_static(JSON_UTF8));
        if self.status == StatusCode::UNAUTHORIZED {
            response.headers_mut().insert(header::WWW_AUTHENTICATE, HeaderValue::from_static("Bearer"));
        }
        response
    }
}

/// PostgREST's table (docs, "Errors from PostgreSQL"), plus `PTxyz` codes for
/// explicit statuses.
pub fn status_for_sqlstate(code: &str) -> StatusCode {
    let status = |n: u16| StatusCode::from_u16(n).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    if let Some(digits) = code.strip_prefix("PT")
        && let Ok(n) = digits.parse::<u16>()
        && let Ok(s) = StatusCode::from_u16(n)
    {
        return s;
    }
    match code {
        "23503" | "23505" => return status(409),
        "25006" => return status(405),
        "42883" | "42P01" => return status(404),
        // PostgREST: 401 for an anonymous request (this API has no logins)
        "42501" => return status(401),
        "42P17" | "53400" => return status(500),
        "P0001" => return status(400),
        _ => {}
    }
    match code.get(..2).unwrap_or("") {
        "08" | "53" => status(503),
        "0L" | "0P" | "28" => status(403),
        "09" | "25" | "2D" | "38" | "39" | "3B" | "40" | "54" | "55" | "57" | "58" | "F0" | "HV" | "P0" | "XX" => {
            status(500)
        }
        _ => status(400),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn statuses_follow_postgrest() {
        assert_eq!(status_for_sqlstate("P0001"), StatusCode::BAD_REQUEST);
        assert_eq!(status_for_sqlstate("P0002"), StatusCode::INTERNAL_SERVER_ERROR);
        assert_eq!(status_for_sqlstate("22P02"), StatusCode::BAD_REQUEST);
        assert_eq!(status_for_sqlstate("42883"), StatusCode::NOT_FOUND);
        assert_eq!(status_for_sqlstate("55000"), StatusCode::INTERNAL_SERVER_ERROR);
        assert_eq!(status_for_sqlstate("57014"), StatusCode::INTERNAL_SERVER_ERROR);
        assert_eq!(status_for_sqlstate("08006"), StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(status_for_sqlstate("PT404"), StatusCode::NOT_FOUND);
        assert_eq!(status_for_sqlstate("PT418").as_u16(), 418);
        assert_eq!(status_for_sqlstate("23505"), StatusCode::CONFLICT);
        assert_eq!(status_for_sqlstate("42501"), StatusCode::UNAUTHORIZED);
    }

    #[test]
    fn body_is_postgrest_shaped() {
        let e = ApiError::new(StatusCode::BAD_REQUEST, "P0001", "Account 'x' does not exist");
        assert_eq!(e.body(), r#"{"code":"P0001","details":null,"hint":null,"message":"Account 'x' does not exist"}"#);
    }
}
