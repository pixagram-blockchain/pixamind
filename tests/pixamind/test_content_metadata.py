"""pixamind.image_info / pixamind.content_metadata: artwork facts read from chain data."""

import json

import pytest
from chain import gif_b64, png_b64, webp_vp8_b64, webp_vp8l_b64, webp_vp8x_b64


def info(sql, body, length=None):
    return sql.one('SELECT pixamind.image_info(%s, %s)', body[:160] if body else body, length)


@pytest.mark.parametrize(
    'body, expected',
    [
        (
            webp_vp8x_b64(64, 48, animated=True, alpha=True),
            {'mime': 'image/webp', 'width': 64, 'height': 48, 'animated': True, 'alpha': True},
        ),
        (
            webp_vp8x_b64(1280, 1280, animated=False, alpha=False),
            {'mime': 'image/webp', 'width': 1280, 'height': 1280, 'animated': False, 'alpha': False},
        ),
        (
            webp_vp8l_b64(32, 16, alpha=True),
            {'mime': 'image/webp', 'width': 32, 'height': 16, 'animated': False, 'alpha': True},
        ),
        (
            webp_vp8_b64(320, 200),
            {'mime': 'image/webp', 'width': 320, 'height': 200, 'animated': False, 'alpha': False},
        ),
        (png_b64(24, 36), {'mime': 'image/png', 'width': 24, 'height': 36, 'animated': False, 'alpha': True}),
        (
            png_b64(24, 36, color_type=2, apng=True),
            {'mime': 'image/png', 'width': 24, 'height': 36, 'animated': True, 'alpha': False},
        ),
        (gif_b64(100, 50), {'mime': 'image/gif', 'width': 100, 'height': 50}),
    ],
)
def test_image_headers(sql, body, expected):
    result = info(sql, body)
    for key, value in expected.items():
        assert result[key] == value, (key, result)
    assert float(result['aspect_ratio']) == pytest.approx(expected['width'] / expected['height'], abs=1e-4)


def test_data_uri_and_size(sql):
    raw = webp_vp8x_b64(16, 16)
    body = 'data:image/webp;base64,' + raw
    result = info(sql, body, len(body))
    assert result['width'] == 16 and result['mime'] == 'image/webp'
    assert result['bytes'] == len(raw) * 3 // 4


@pytest.mark.parametrize(
    'body', [None, '', 'hello world, not an image', '@@ -1,5 +1,5 @@ diff patch', 'data:nocomma', 'QUJD' * 20]
)
def test_not_an_image(sql, body):
    assert info(sql, body) is None


def test_pixagram_artwork_metadata(sql):
    meta = {
        'app': 'pixagram/3.0.2',
        'format': 'image',
        'tags': ['PixelArt', 'cat', '', 7, 'retro'],
        'image': [],
        'description': 'A cat.',
        'nsfw': False,
        'oc': True,
        'ai': '',
        'license': {
            'type': 'PIXA_LICENSE',
            'version': '1.0',
            'royaltyPercentage': 5,
            'isCustomized': False,
            'rightsConfiguration': {'holderRights': {}, 'visitorRights': {}},
            'governingLaw': {'jurisdiction': ''},
        },
    }
    body = webp_vp8x_b64(128, 96)
    result = sql.one('SELECT pixamind.content_metadata(%s, %s, %s)', json.dumps(meta), body[:160], len(body))
    assert result['format'] == 'image'
    assert result['app'] == 'pixagram/3.0.2'
    assert result['tags'] == ['pixelart', 'cat', 'retro']
    assert result['description'] == 'A cat.'
    assert result['nsfw'] is False and result['original_content'] is True
    assert 'ai_used' not in result  # "" means no AI
    assert result['license'] == {
        'type': 'PIXA_LICENSE',
        'version': '1.0',
        'royalty_percentage': 5,
        'is_customized': False,
    }
    assert result['image']['width'] == 128 and result['image']['height'] == 96


def test_ai_disclosure_and_text_posts(sql):
    ai = sql.one(
        'SELECT pixamind.content_metadata(%s, %s)', json.dumps({'format': 'image', 'ai': ' flux-pixel '}), None
    )
    assert ai['ai_used'] == 'flux-pixel'
    text = sql.one(
        'SELECT pixamind.content_metadata(%s, %s)', json.dumps({'app': 'pixagram', 'format': 'text'}), 'Hello'
    )
    assert text == {'app': 'pixagram', 'format': 'text'}
    broken = sql.one('SELECT pixamind.content_metadata(%s, %s)', '{not json', None)
    assert broken == {}
