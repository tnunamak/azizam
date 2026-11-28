from gateway import asset_types
import yaml
from pathlib import Path

OLD = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif', '.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.flac': 'audio/flac', '.ogg': 'audio/ogg', '.m4a': 'audio/mp4', '.aac': 'audio/aac', '.mp4': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime', '.mkv': 'video/x-matroska', '.glb': 'model/gltf-binary', '.gltf': 'model/gltf+json', '.obj': 'model/obj', '.stl': 'model/stl', '.ply': 'model/ply', '.usdz': 'model/vnd.usdz+zip', '.safetensors': 'adapter/safetensors'}

def test_shipped_asset_format_parity():
    assert asset_types.SUFFIX_MEDIA_TYPES == OLD
    for suffix, mime in OLD.items():
        assert asset_types.media_type_for_suffix(suffix) == mime
        assert asset_types.kind_for_media_type(mime) == mime.split('/')[0]

def test_data_only_suffix(tmp_path, monkeypatch):
    path = tmp_path / 'formats.yml'
    path.write_text(yaml.safe_dump({'asset_suffixes': {'.custom': 'model/custom'}}))
    monkeypatch.setattr(asset_types, 'SUFFIX_MEDIA_TYPES', asset_types.load_suffix_media_types(path))
    assert asset_types.media_type_for_suffix('.CUSTOM') == 'model/custom'
    assert asset_types.suffix_for_media_type('model/custom') == '.custom'
    assert asset_types.kind_for_media_type(asset_types.media_type_for_suffix('.custom')) == 'model'
    assert asset_types.download_file_name('result', None, 'model/custom') == 'result.custom'
