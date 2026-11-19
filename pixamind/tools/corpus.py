"""`pixamind corpus`: build a JSON-RPC request corpus (JSON lines) for apidiff and bench.

    pixamind corpus from-tavern --tests-dir tests/api_tests/hivemind/tavern --out hive.jsonl
        [--only bridge_api_patterns,condenser_api_patterns] [--no-negative]
        Every request of the inherited API test suite (Hive mainnet data). Needs PyYAML.

    pixamind corpus sample --url https://pixa-api --out pixa.jsonl [--posts 40] [--seed 7]
        Requests built from what a live Pixa node returns right now: recent, trending and
        promoted posts, their authors, communities and tags, across the read methods
        Pixagram uses. Re-run it to refresh; the output is deterministic for a given seed
        and node state.

Each line: {"name": "...", "method": "bridge.get_post", "params": {...}}
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

from pixamind.tools.jsonrpc import RpcClient


# ------------------------------------------------------------- from-tavern
def _yaml_loader():
    try:
        import yaml
    except ImportError as ex:  # pragma: no cover - dev dependency
        raise SystemExit('pixamind corpus from-tavern needs PyYAML (pip install pyyaml)') from ex

    class Loader(yaml.SafeLoader):
        pass

    # tavern files use custom tags such as !include; keep their value, ignore the tag
    def any_tag(loader, _suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    Loader.add_multi_constructor('!', any_tag)
    return yaml, Loader


def from_tavern(tests_dir: Path, only: list[str] | None, negative: bool) -> list[dict]:
    yaml, Loader = _yaml_loader()
    entries = []
    for path in sorted(tests_dir.rglob('*.tavern.yaml')):
        rel = path.relative_to(tests_dir).as_posix()
        if only and not any(rel.startswith(prefix) for prefix in only):
            continue
        if not negative and 'negative' in rel:
            continue
        try:
            docs = list(yaml.load_all(path.read_text(), Loader=Loader))
        except yaml.YAMLError:
            continue
        for doc in docs:
            for stage in (doc or {}).get('stages', []) or []:
                body = ((stage or {}).get('request') or {}).get('json')
                if isinstance(body, dict) and isinstance(body.get('method'), str):
                    entry = {'name': rel.removesuffix('.tavern.yaml'), 'method': body['method']}
                    if 'params' in body:
                        entry['params'] = body['params']
                    entries.append(entry)
    return entries


# ------------------------------------------------------------------ sample
def sample(client: RpcClient, posts_wanted: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    posts: dict[tuple[str, str], dict] = {}
    for sort in ('created', 'trending', 'hot', 'promoted'):
        try:
            for post in client.result('bridge.get_ranked_posts', {'sort': sort, 'limit': 20}) or []:
                posts.setdefault((post['author'], post['permlink']), post)
        except RuntimeError:
            continue  # e.g. a Hivemind node without the promoted sort
    if not posts:
        raise RuntimeError(f'{client.url} returned no posts; is it a Pixamind/Hivemind API?')
    chosen = rng.sample(sorted(posts), min(posts_wanted, len(posts)))
    authors = sorted({a for a, _ in chosen})
    communities = sorted({p.get('community') for p in posts.values() if p.get('community')})
    tags = sorted({p.get('category') for p in posts.values() if p.get('category')})
    observer = authors[0]

    entries: list[dict] = []

    def add(name: str, method: str, params) -> None:
        entries.append({'name': name, 'method': method, 'params': params})

    add('info', 'pixamind_api.get_info', {})
    add('info', 'hive.get_info', {})
    for sort in ('created', 'trending', 'hot', 'promoted', 'payout', 'payout_comments', 'muted'):
        add('ranked', 'bridge.get_ranked_posts', {'sort': sort, 'limit': 20})
        add('ranked observer', 'bridge.get_ranked_posts', {'sort': sort, 'limit': 20, 'observer': observer})
    for tag in tags[:5]:
        add('ranked tag', 'bridge.get_ranked_posts', {'sort': 'trending', 'tag': tag, 'limit': 20})
        add('condenser tag', 'condenser_api.get_discussions_by_created', {'tag': tag, 'limit': 20})
    for community in communities[:5]:
        add('community', 'bridge.get_community', {'name': community, 'observer': observer})
        add('community posts', 'bridge.get_ranked_posts', {'sort': 'created', 'tag': community, 'limit': 20})
        add('subscribers', 'bridge.list_subscribers', {'community': community, 'limit': 50})
        add('roles', 'bridge.list_community_roles', {'community': community})
    add('communities', 'bridge.list_communities', {'limit': 50})
    add('communities', 'bridge.list_pop_communities', {'limit': 25})
    add('topics', 'bridge.get_trending_topics', {'limit': 20})
    for author, permlink in chosen:
        add('post', 'bridge.get_post', {'author': author, 'permlink': permlink, 'observer': observer})
        add('discussion', 'bridge.get_discussion', {'author': author, 'permlink': permlink})
        add('content', 'condenser_api.get_content', [author, permlink])
        add('replies', 'condenser_api.get_content_replies', [author, permlink])
        add('votes', 'condenser_api.get_active_votes', [author, permlink])
        add(
            'votes',
            'database_api.list_votes',
            {'start': [author, permlink, ''], 'limit': 100, 'order': 'by_comment_voter'},
        )
        add('metadata', 'pixamind_content.get_content_metadata', {'author': author, 'permlink': permlink})
        add('promotions', 'pixamind_feed.get_promotions', {'author': author, 'permlink': permlink})
    add('metadata batch', 'pixamind_content.get_content_metadata', {'posts': [[a, p] for a, p in chosen[:20]]})
    add('comments', 'database_api.find_comments', {'comments': [[a, p] for a, p in chosen[:20]]})
    for author in authors:
        add('profile', 'bridge.get_profile', {'account': author, 'observer': observer})
        for sort in ('blog', 'posts', 'comments', 'replies', 'feed'):
            add(f'account posts {sort}', 'bridge.get_account_posts', {'sort': sort, 'account': author, 'limit': 20})
        add('followers', 'condenser_api.get_followers', [author, None, 'blog', 50])
        add('following', 'condenser_api.get_following', [author, None, 'blog', 50])
        add('follow count', 'condenser_api.get_follow_count', [author])
        add('blog', 'condenser_api.get_blog', [author, 0, 20])
        add('notifications', 'bridge.account_notifications', {'account': author, 'limit': 50})
        add('unread', 'bridge.unread_notifications', {'account': author})
        add('relationship', 'bridge.get_relationship_between_accounts', [observer, author])
        add('mutual', 'pixamind_graph.get_mutual_follows', {'account': author})
        add('subscriptions', 'bridge.list_all_subscriptions', {'account': author})
    add('relationships', 'pixamind_graph.get_relationships', {'observer': observer, 'accounts': authors[:100]})
    add('profiles', 'bridge.get_profiles', {'accounts': authors[:20], 'observer': observer})
    return entries


def write(entries: list[dict], out: Path) -> None:
    with out.open('w') as fh:
        for entry in entries:
            fh.write(json.dumps(entry, sort_keys=True) + '\n')


def read(path: Path) -> list[dict]:
    entries = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        entry = json.loads(line)
        if not isinstance(entry.get('method'), str):
            raise ValueError(f'{path}:{number}: missing "method"')
        entry.setdefault('name', entry['method'])
        entries.append(entry)
    return entries


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog='pixamind corpus', description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    t = sub.add_parser('from-tavern', help='requests of the inherited API test suite')
    t.add_argument('--tests-dir', type=Path, default=Path('tests/api_tests/hivemind/tavern'))
    t.add_argument('--only', help='comma-separated path prefixes, e.g. bridge_api_patterns')
    t.add_argument('--no-negative', action='store_true', help='skip the *_negative suites')
    t.add_argument('--out', type=Path, required=True)
    s = sub.add_parser('sample', help='requests built from a live node')
    s.add_argument('--url', required=True)
    s.add_argument('--posts', type=int, default=40)
    s.add_argument('--seed', type=int, default=7)
    s.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == 'from-tavern':
        only = [p.strip() for p in args.only.split(',')] if args.only else None
        entries = from_tavern(args.tests_dir, only, negative=not args.no_negative)
    else:
        client = RpcClient(args.url)
        try:
            entries = sample(client, args.posts, args.seed)
        except RuntimeError as ex:
            print(ex, file=sys.stderr)
            return 1
        finally:
            client.close()
    write(entries, args.out)
    print(f'{len(entries)} requests written to {args.out}')
    return 0
