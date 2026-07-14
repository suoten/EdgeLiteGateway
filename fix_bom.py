"""Remove all leading BOM (U+FEFF) characters from test files."""
import os

BOM = b'\xef\xbb\xbf'

files_to_fix = [
    'tests/test_api_mcp.py',
    'tests/test_cache_ext.py',
    'tests/test_api_auth.py',
    'tests/test_api_devices.py',
    'tests/test_api_grafana.py',
    'tests/test_api_metrics.py',
    'tests/test_data_service.py',
    'tests/test_video_service.py',
]

for fn in files_to_fix:
    path = os.path.join(os.path.dirname(__file__), fn)
    if not os.path.exists(path):
        continue
    with open(path, 'rb') as f:
        data = f.read()
    # Remove all leading BOM markers
    while data.startswith(BOM):
        data = data[3:]
    with open(path, 'wb') as f:
        f.write(data)
    print(f'Fixed: {fn}')

print('Done!')
