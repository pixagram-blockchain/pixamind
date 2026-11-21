//! Prometheus metrics, served on the admin listener at /metrics.

use std::collections::HashSet;
use std::sync::Mutex;
use std::time::Duration;

use prometheus::{
    Encoder, HistogramOpts, HistogramVec, IntCounterVec, IntGauge, IntGaugeVec, Opts, Registry, TextEncoder,
};

/// JSON-RPC method labels kept before new ones are counted as "other", so a
/// client sending random method names can't grow the metrics without bound.
const MAX_METHOD_LABELS: usize = 512;

pub struct Metrics {
    registry: Registry,
    requests: IntCounterVec,
    duration: HistogramVec,
    pub in_flight: IntGauge,
    pool: IntGaugeVec,
    functions: IntGauge,
    methods: Mutex<HashSet<String>>,
}

impl Metrics {
    pub fn new() -> Self {
        let registry = Registry::new();
        let requests = IntCounterVec::new(
            Opts::new("pixamind_api_requests_total", "Requests by function, JSON-RPC method and HTTP status"),
            &["function", "method", "status"],
        )
        .expect("valid metric");
        let duration = HistogramVec::new(
            HistogramOpts::new(
                "pixamind_api_request_duration_seconds",
                "Time to answer a request, by function and JSON-RPC method",
            )
            .buckets(vec![0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0]),
            &["function", "method"],
        )
        .expect("valid metric");
        let in_flight =
            IntGauge::new("pixamind_api_requests_in_flight", "Requests being answered").expect("valid metric");
        let pool = IntGaugeVec::new(
            Opts::new("pixamind_api_db_connections", "Database pool: size, available, waiting"),
            &["state"],
        )
        .expect("valid metric");
        let functions = IntGauge::new("pixamind_api_functions", "Functions in the schema cache").expect("valid metric");
        for collector in [
            Box::new(requests.clone()) as Box<dyn prometheus::core::Collector>,
            Box::new(duration.clone()),
            Box::new(in_flight.clone()),
            Box::new(pool.clone()),
            Box::new(functions.clone()),
        ] {
            registry.register(collector).expect("unique metric");
        }
        Self { registry, requests, duration, in_flight, pool, functions, methods: Mutex::new(HashSet::new()) }
    }

    /// A JSON-RPC method as a label: plausible names until the cap, then "other".
    pub fn method_label(&self, method: Option<&str>) -> String {
        let Some(method) = method else { return String::new() };
        let plausible = !method.is_empty()
            && method.len() <= 80
            && method.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'.');
        if !plausible {
            return "other".into();
        }
        let mut seen = self.methods.lock().unwrap_or_else(|e| e.into_inner());
        if seen.contains(method) || (seen.len() < MAX_METHOD_LABELS && seen.insert(method.to_string())) {
            method.to_string()
        } else {
            "other".into()
        }
    }

    pub fn observe(&self, function: &str, method: &str, status: u16, elapsed: Duration) {
        self.requests.with_label_values(&[function, method, status.to_string().as_str()]).inc();
        self.duration.with_label_values(&[function, method]).observe(elapsed.as_secs_f64());
    }

    pub fn set_pool(&self, size: usize, available: usize, waiting: usize) {
        self.pool.with_label_values(&["size"]).set(size as i64);
        self.pool.with_label_values(&["available"]).set(available as i64);
        self.pool.with_label_values(&["waiting"]).set(waiting as i64);
    }

    pub fn set_functions(&self, count: usize) {
        self.functions.set(count as i64);
    }

    pub fn render(&self) -> String {
        let mut buffer = Vec::new();
        TextEncoder::new().encode(&self.registry.gather(), &mut buffer).expect("text encoding");
        String::from_utf8(buffer).unwrap_or_default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn method_labels_are_bounded() {
        let m = Metrics::new();
        assert_eq!(m.method_label(Some("bridge.get_post")), "bridge.get_post");
        assert_eq!(m.method_label(Some("drop table; --")), "other");
        assert_eq!(m.method_label(None), "");
        for i in 0..MAX_METHOD_LABELS {
            m.method_label(Some(&format!("api.m{i}")));
        }
        assert_eq!(m.method_label(Some("api.one_too_many")), "other");
        assert_eq!(m.method_label(Some("bridge.get_post")), "bridge.get_post");
    }

    #[test]
    fn renders_text_format() {
        let m = Metrics::new();
        m.observe("home", "bridge.get_post", 200, Duration::from_millis(3));
        let text = m.render();
        assert!(
            text.contains(r#"pixamind_api_requests_total{function="home",method="bridge.get_post",status="200"} 1"#),
            "{text}"
        );
    }
}
