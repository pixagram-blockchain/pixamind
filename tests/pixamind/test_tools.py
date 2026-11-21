"""Operator tools (no database needed; HTTP tools run against a fake JSON-RPC server)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from pixamind import cli
from pixamind.tools import apidiff, bench, corpus, info, sync_report

REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------- fake JSON-RPC node
class FakeNode:
    """Answers JSON-RPC from a dict {method: result or callable(params)}."""

    def __init__(self, answers: dict):
        self.answers = answers
        node = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_POST(self):  # noqa: N802 - http.server API
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                answer = node.answers.get(request['method'])
                if answer is None:
                    body = {
                        'jsonrpc': '2.0',
                        'id': request['id'],
                        'error': {'code': -32601, 'message': 'no such method'},
                    }
                else:
                    result = answer(request.get('params')) if callable(answer) else answer
                    body = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}/'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def node_factory():
    nodes = []

    def make(answers):
        node = FakeNode(answers)
        nodes.append(node)
        return node

    yield make
    for node in nodes:
        node.close()


# ------------------------------------------------------------------------ cli
def test_cli_help_version_and_unknown(capsys):
    assert cli.main(['--help']) == 0
    assert 'pixamind integrity' in capsys.readouterr().out
    assert cli.main(['--version']) == 0
    assert 'hivemind 1.28.8' in capsys.readouterr().out
    assert cli.main(['frobnicate']) == 2


def test_console_script_runs(tmp_path):
    script = shutil.which('pixamind') or str(Path(sys.executable).with_name('pixamind'))
    if not Path(script).exists():
        pytest.skip('pixamind is not installed in this environment')
    out = subprocess.run([script, '--version'], capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0 and out.stdout.startswith('pixamind ')


# -------------------------------------------------------------- sync-report
LOG = """\
INFO - hive.indexer.blocks:350 - [PHASE-SUMMARY] blocks=1-1000 total=10.000s load=2.000 posts=5.000 votes=2.500 pixamind=0.500
INFO - hive.indexer.blocks:350 - [PHASE-SUMMARY] blocks=1001-2000 total=20.000s load=4.000 posts=10.000 votes=5.000 pixamind=1.000
noise line
INFO - hive.indexer.blocks:452 - [PROCESS LIVE SQL] blocks 2001-2001 in 0.0400s
INFO - hive.indexer.blocks:452 - [PROCESS LIVE SQL] blocks 2002-2002 in 0.2000s
"""


def test_sync_report():
    rep = sync_report.report(sync_report.parse(LOG.splitlines()), top=1)
    massive = rep['massive']
    assert massive['blocks'] == 2000 and massive['blocks_per_second'] == pytest.approx(66.67, abs=0.01)
    assert list(massive['phases']) == ['posts', 'votes', 'load', 'pixamind']
    assert massive['phases']['posts']['share'] == 0.5
    assert massive['slowest'][0]['first'] == 1001
    assert rep['live']['blocks'] == 2 and rep['live']['max_seconds'] == 0.2
    text = sync_report.render(rep)
    assert '| posts | 15.0 | 50.0% |' in text
    assert sync_report.render({}).startswith('no ')


# -------------------------------------------------------------------- bench
def test_percentile_and_summary():
    values = sorted([0.1, 0.2, 0.3, 0.4, 1.0])
    assert bench.percentile(values, 50) == 0.3
    assert bench.percentile(values, 95) == 1.0
    assert bench.percentile([], 50) is None
    summary = bench.summarize({'a.b': [(0.01, True), (0.03, False)]}, wall_seconds=1.0)
    assert summary['requests'] == 2 and summary['errors'] == 1 and summary['methods']['a.b']['max_ms'] == 30.0


def test_bench_against_node(node_factory, tmp_path):
    node = node_factory({'bridge.get_post': {'author': 'alice'}, 'hive.get_info': {}})
    path = tmp_path / 'c.jsonl'
    corpus.write(
        [{'name': 'p', 'method': 'bridge.get_post', 'params': {}}, {'name': 'i', 'method': 'hive.get_info'}], path
    )
    report = bench.run(node.url, corpus.read(path), concurrency=2, duration=None, rounds=3, warmup=1)
    assert report['requests'] == 6 and report['errors'] == 0
    assert set(report['methods']) == {'bridge.get_post', 'hive.get_info'}
    assert '| bridge.get_post |' in bench.markdown(report)


# ------------------------------------------------------------------ apidiff
def test_normalize_and_differences():
    ignores = [p.split('.') for p in apidiff.DEFAULT_IGNORES + ['result.*.ts']]
    a = {'id': 1, 'jsonrpc': '2.0', 'result': [{'x': 1, 'ts': 'now'}, {'x': 2, 'ts': 'later'}]}
    b = {'id': 9, 'jsonrpc': '2.0', 'result': [{'x': 1, 'ts': 'then'}, {'x': 3, 'ts': 'never'}]}
    na, nb = apidiff.normalize(a, ignores), apidiff.normalize(b, ignores)
    assert na == {'result': [{'x': 1}, {'x': 2}]}
    assert apidiff.differences(na, nb) == ['result[1].x: 2 != 3']
    assert apidiff.differences({'a': 1}, {'b': 1}) == ['a: only left = 1', 'b: only right = 1']


def test_apidiff_two_nodes(node_factory, tmp_path):
    left = node_factory({'m.same': {'v': 1}, 'm.diff': {'v': 1}})
    right = node_factory({'m.same': {'v': 1}, 'm.diff': {'v': 2}})
    entries = [{'name': 'same', 'method': 'm.same'}, {'name': 'diff', 'method': 'm.diff', 'params': [1]}]
    report = apidiff.run(left.url, right.url, entries, apidiff.DEFAULT_IGNORES, concurrency=2, show=3)
    assert report['requests'] == 2 and report['differing'] == 1
    assert report['differences'][0]['differences'] == ['result.v: 1 != 2']
    path = tmp_path / 'c.jsonl'
    corpus.write(entries, path)
    assert apidiff.main(['--left', left.url, '--right', left.url, '--corpus', str(path)]) == 0
    assert apidiff.main(['--left', left.url, '--right', right.url, '--corpus', str(path)]) == 1


# ------------------------------------------------------------------- corpus
def test_corpus_from_tavern():
    tests_dir = REPO / 'tests' / 'api_tests' / 'hivemind' / 'tavern'
    entries = corpus.from_tavern(tests_dir, only=['bridge_api_patterns/get_ranked_posts'], negative=True)
    assert len(entries) > 20
    assert all(e['method'] == 'bridge.get_ranked_posts' for e in entries)
    everything = corpus.from_tavern(tests_dir, only=None, negative=False)
    assert len(everything) > 1000
    assert not any('negative' in e['name'] for e in everything)


def test_corpus_read_rejects_bad_lines(tmp_path):
    path = tmp_path / 'bad.jsonl'
    path.write_text('# comment\n\n{"params": {}}\n')
    with pytest.raises(ValueError):
        corpus.read(path)


def test_corpus_sample(node_factory):
    posts = [
        {'author': 'alice', 'permlink': 'cat', 'category': 'pixelart', 'community': 'portal-123456'},
        {'author': 'bob', 'permlink': 'dog', 'category': 'pixelart'},
    ]
    node = node_factory({'bridge.get_ranked_posts': posts})
    from pixamind.tools.jsonrpc import RpcClient

    client = RpcClient(node.url)
    entries = corpus.sample(client, posts_wanted=5, seed=1)
    client.close()
    methods = {e['method'] for e in entries}
    assert {
        'bridge.get_post',
        'pixamind_content.get_content_metadata',
        'bridge.get_community',
        'pixamind_graph.get_relationships',
    } <= methods
    assert {
        'name': 'community',
        'method': 'bridge.get_community',
        'params': {'name': 'portal-123456', 'observer': 'alice'},
    } in entries
    again = corpus.sample(RpcClient(node.url), posts_wanted=5, seed=1)
    assert again == entries


# --------------------------------------------------------------------- info
def test_info_render(node_factory, capsys):
    node = node_factory(
        {
            'pixamind_api.get_info': {
                'version': '0.1.0',
                'hivemind_base': '1.28.8',
                'chain': {'name': 'pixa', 'liquid_symbol': 'PIXA', 'stable_symbol': 'PXS'},
            },
            'pixamind_api.get_metrics': {
                'context': {'processed_block': 100, 'irreversible_block': 99, 'stage': 'live', 'forking': True},
                'lag': {'blocks_behind_head': 1},
                'window_minutes': 10,
                'throughput': {'blocks_per_second': 0.33},
                'live_latency_seconds': {'p50': 1.2, 'p95': 2.5, 'samples': 200},
                'events': {'enabled': True},
                'promoted_posts': 3,
                'failures': {
                    'open': 1,
                    'latest': [{'projection': 'events', 'first_block': 5, 'last_block': 5, 'error': 'boom'}],
                },
                'phase_seconds_avg': {'load': 0.01},
            },
        }
    )
    assert info.main(['--url', node.url]) == 0
    out = capsys.readouterr().out
    assert 'Pixamind 0.1.0 (hivemind 1.28.8) on pixa [PIXA/PXS]' in out
    assert 'p50 1.2 s, p95 2.5 s' in out and 'events 5..5: boom' in out
    assert 'Feed cache   -' in out and 'JIT          - in API calls' in out  # a node without them
    broken = node_factory({})
    assert info.main(['--url', broken.url]) == 1
    text = info.render(
        {
            'database': {'jit': {'api': 'off', 'database': None}},
            'feed_cache': {'enabled': True, 'targets': 6, 'fresh': 5, 'failing': 1, 'block': 100},
        },
        {},
    )
    assert 'Feed cache   on, 5/6 targets fresh at block 100, 1 failing' in text
    assert 'JIT          off in API calls, database default -' in text
    assert 'standby: not served' in info.render({'feed_cache': {'enabled': True, 'targets': 6, 'standby': True}}, {})
