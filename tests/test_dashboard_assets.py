"""The redesigned dashboard must receive real local assets, not an HTML fallback."""

from pathlib import Path
import re

from httpx import AsyncClient

from treg import api


async def test_dashboard_redesign_assets_are_served_from_the_same_origin(clients: AsyncClient):
    page = await clients.get('/app')
    assert page.status_code == 200
    paths = set(re.findall(r'(?:href|src)="(/media/redesign/[^"\']+)"', page.text))
    assert '/media/redesign/dashboard.css' in paths
    # Include the dynamic task, provider and token-state images as well as literal URLs.
    asset_dir = Path(api.__file__).parent / 'web' / 'media' / 'redesign'
    paths.update('/media/redesign/' + p.name for p in asset_dir.iterdir() if p.suffix in {'.svg', '.png', '.jpg'})
    for path in sorted(paths):
        response = await clients.get(path)
        assert response.status_code == 200, path
        assert response.content, path
        expected_type = 'text/css' if path.endswith('.css') else 'image/'
        assert response.headers['content-type'].startswith(expected_type), path
