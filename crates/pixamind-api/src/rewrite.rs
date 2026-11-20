//! The nginx rewrite rules that map public REST paths to SQL functions.
//!
//! Pixamind's `rewrite_rules.conf` is generated from the OpenAPI comments of the
//! SQL endpoints (`scripts/openapi_rewrite.sh`) and was included by the nginx
//! rewriter in front of PostgREST. The server reads the same file, so the routes
//! stay defined in one place. Only the subset those files use is supported:
//! `rewrite <regex> <replacement> [break|last];` with `$1`..`$9` captures.

use regex::Regex;

/// The rules shipped with this repository.
pub const BUNDLED: &str = include_str!("../../../rewrite_rules.conf");

#[derive(Debug)]
struct Rule {
    pattern: Regex,
    path: String,
    /// Arguments of the replacement, `key=value` templates.
    args: Vec<(String, String)>,
    /// nginx appends the request's own arguments unless the replacement ends with `?`.
    keep_request_args: bool,
}

#[derive(Debug)]
pub struct RewriteRules {
    rules: Vec<Rule>,
}

/// Where a request path leads after the rules.
#[derive(Debug, PartialEq, Eq)]
pub struct Rewritten {
    pub path: String,
    pub args: Vec<(String, String)>,
    pub keep_request_args: bool,
}

impl RewriteRules {
    pub fn parse(text: &str) -> Result<Self, String> {
        let mut rules = Vec::new();
        for (index, raw) in text.lines().enumerate() {
            let line_no = index + 1;
            let line = strip_comment(raw).trim();
            if line.is_empty() {
                continue;
            }
            let statement = line
                .strip_suffix(';')
                .ok_or_else(|| format!("line {line_no}: expected one statement ending with ';'"))?;
            let tokens = tokenize(statement).map_err(|e| format!("line {line_no}: {e}"))?;
            let (directive, rest) = tokens.split_first().ok_or_else(|| format!("line {line_no}: empty statement"))?;
            if directive != "rewrite" {
                return Err(format!("line {line_no}: unsupported directive {directive:?}"));
            }
            let (regex, replacement, flag) = match rest {
                [regex, replacement] => (regex, replacement, None),
                [regex, replacement, flag] => (regex, replacement, Some(flag.as_str())),
                _ => return Err(format!("line {line_no}: expected `rewrite <regex> <replacement> [flag]`")),
            };
            match flag {
                None | Some("break") | Some("last") => {}
                Some(other) => return Err(format!("line {line_no}: unsupported flag {other:?}")),
            }
            let pattern = Regex::new(regex).map_err(|e| format!("line {line_no}: {e}"))?;
            check_variables(replacement, pattern.captures_len() - 1).map_err(|e| format!("line {line_no}: {e}"))?;
            let (path, query) = match replacement.split_once('?') {
                Some((path, query)) => (path, Some(query)),
                None => (replacement.as_str(), None),
            };
            let args = query
                .unwrap_or("")
                .split('&')
                .filter(|pair| !pair.is_empty())
                .map(|pair| match pair.split_once('=') {
                    Some((k, v)) => (k.to_string(), v.to_string()),
                    None => (pair.to_string(), String::new()),
                })
                .collect();
            rules.push(Rule { pattern, path: path.to_string(), args, keep_request_args: !replacement.ends_with('?') });
        }
        Ok(Self { rules })
    }

    pub fn len(&self) -> usize {
        self.rules.len()
    }

    /// The first rule matching `path` (already percent-decoded), as nginx applies them.
    pub fn apply(&self, path: &str) -> Option<Rewritten> {
        self.rules.iter().find_map(|rule| {
            let captures = rule.pattern.captures(path)?;
            let expand = |template: &str| expand(template, &captures);
            Some(Rewritten {
                path: expand(&rule.path),
                args: rule.args.iter().map(|(k, v)| (expand(k), expand(v))).collect(),
                keep_request_args: rule.keep_request_args,
            })
        })
    }
}

fn strip_comment(line: &str) -> &str {
    let mut quote = None;
    for (i, c) in line.char_indices() {
        match (quote, c) {
            (None, '#') => return &line[..i],
            (None, '\'' | '"') => quote = Some(c),
            (Some(q), _) if c == q => quote = None,
            _ => {}
        }
    }
    line
}

fn tokenize(statement: &str) -> Result<Vec<String>, String> {
    let mut tokens = Vec::new();
    let mut chars = statement.chars().peekable();
    while let Some(&c) = chars.peek() {
        if c.is_whitespace() {
            chars.next();
            continue;
        }
        let mut token = String::new();
        if c == '\'' || c == '"' {
            chars.next();
            loop {
                match chars.next() {
                    Some(ch) if ch == c => break,
                    Some(ch) => token.push(ch),
                    None => return Err("unterminated quote".into()),
                }
            }
        } else {
            while let Some(&ch) = chars.peek() {
                if ch.is_whitespace() {
                    break;
                }
                token.push(ch);
                chars.next();
            }
        }
        tokens.push(token);
    }
    Ok(tokens)
}

/// Only `$1`..`$9` are known here; nginx variables like `$args` are not.
fn check_variables(replacement: &str, groups: usize) -> Result<(), String> {
    let mut chars = replacement.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '$' {
            match chars.peek().copied() {
                Some(d) if d.is_ascii_digit() => {
                    let n = d.to_digit(10).unwrap_or(0) as usize;
                    if n == 0 || n > groups {
                        return Err(format!("${n} has no capture group"));
                    }
                    chars.next();
                }
                _ => {
                    let name: String = chars.clone().take_while(|c| c.is_ascii_alphanumeric() || *c == '_').collect();
                    return Err(format!("unsupported nginx variable ${name}"));
                }
            }
        }
    }
    Ok(())
}

fn expand(template: &str, captures: &regex::Captures<'_>) -> String {
    let mut out = String::with_capacity(template.len());
    let mut chars = template.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '$'
            && let Some(n) = chars.peek().and_then(|d| d.to_digit(10))
        {
            chars.next();
            out.push_str(captures.get(n as usize).map_or("", |m| m.as_str()));
            continue;
        }
        out.push(c);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rules() -> RewriteRules {
        RewriteRules::parse(BUNDLED).expect("bundled rules parse")
    }

    #[test]
    fn bundled_rules_route_the_rest_api() {
        let r = rules();
        assert_eq!(
            r.apply("/accounts/alice/operations"),
            Some(Rewritten {
                path: "/rpc/get_ops_by_account".into(),
                args: vec![("account-name".into(), "alice".into())],
                keep_request_args: true,
            })
        );
        assert_eq!(r.apply("/sync-status").unwrap().path, "/rpc/get_sync_status");
        assert_eq!(r.apply("/blog/reblogs").unwrap().path, "/rpc/get_reblogs");
        assert_eq!(
            r.apply("/accounts/bob/pending-author-rewards").unwrap().args,
            vec![("account-name".to_string(), "bob".to_string())]
        );
    }

    #[test]
    fn everything_else_is_json_rpc() {
        let r = rules();
        for path in ["/", "/rpc", "/anything/else"] {
            let rewritten = r.apply(path).unwrap();
            assert_eq!(rewritten.path, "/rpc/home", "{path}");
            assert!(rewritten.args.is_empty());
        }
    }

    #[test]
    fn hafah_style_rules_parse() {
        let text = "rewrite ^/blocks/([^/]+)/header /rpc/get_block_header?block-num=$1 break;\n\
                    # a comment\n\
                    rewrite ^/$ / break;\n\
                    rewrite ^/(.*)$ /rpc/$1 break;\n";
        let r = RewriteRules::parse(text).unwrap();
        assert_eq!(r.len(), 3);
        assert_eq!(r.apply("/blocks/42/header").unwrap().args, vec![("block-num".into(), "42".into())]);
        assert_eq!(r.apply("/").unwrap().path, "/");
        assert_eq!(r.apply("/get_version").unwrap().path, "/rpc/get_version");
    }

    #[test]
    fn trailing_question_mark_drops_request_args() {
        let r = RewriteRules::parse("rewrite ^/x /rpc/x? break;").unwrap();
        assert!(!r.apply("/x").unwrap().keep_request_args);
    }

    #[test]
    fn unsupported_syntax_is_rejected() {
        assert!(RewriteRules::parse("location / { }").is_err());
        assert!(RewriteRules::parse("rewrite ^/a /b permanent;").is_err());
        assert!(RewriteRules::parse("rewrite ^/a /b?f=$path_filters break;").is_err());
        assert!(RewriteRules::parse("rewrite ^/a /b?x=$1 break;").is_err()); // no group 1
        assert!(RewriteRules::parse("rewrite ^/a /b break").is_err()); // no ';'
    }

    #[test]
    fn quoted_regex_and_comments() {
        let r = RewriteRules::parse("rewrite \"^/a#b\" /rpc/x break; # trailing comment").unwrap();
        assert_eq!(r.apply("/a#b").unwrap().path, "/rpc/x");
    }
}
