import gzip
import zlib

import pytest

from treg.application import table as table_app


@pytest.mark.parametrize("cut", [1, 8, 12])
def test_truncated_gzip_falls_back_to_original_bytes(cut):
    body = gzip.compress(b'{"name":"Ada"}')[:-cut]
    assert table_app._decoded(body, "gzip") == body


def test_raw_deflate_decodes_like_zlib_wrapped_deflate():
    body = b'{"name":"Ada"}'
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    compressed = compressor.compress(body) + compressor.flush()
    assert table_app._decoded(compressed, "deflate") == body
    assert table_app._decoded(zlib.compress(body), "deflate") == body
