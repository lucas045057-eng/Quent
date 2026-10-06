from dataclasses import replace
from decimal import Decimal as D
import json
import pytest

from quant_features.core import project_features
from quant_research.storage import export_features, load_features
from tests.quant_research.test_features import observation


def test_parquet_export_is_immutable_reproducible_and_keeps_postgres_reference(tmp_path):
    features = project_features((observation(),replace(observation('PRICE',value=D('50000')),canonical_symbol='ETH-USDT-PERP')))
    first = export_features(features,tmp_path,postgres_source_ref='postgres:quant_phase9_test/fixture/snapshot')
    second = export_features(features,tmp_path,postgres_source_ref='postgres:quant_phase9_test/fixture/snapshot')
    assert first == second
    manifest = json.loads(first.read_text())
    assert manifest['postgres_source_ref'] == 'postgres:quant_phase9_test/fixture/snapshot'
    assert manifest['coverage']['SYNTHETIC_FIXTURE'] == 2
    assert manifest['coverage']['POINT_IN_TIME'] == 0
    assert load_features(first) == features
    data = tmp_path / manifest['data_file']
    data.write_bytes(data.read_bytes() + b'tampered')
    with pytest.raises(ValueError,match='digest'):
        load_features(first)


def test_manifest_path_traversal_is_rejected_before_opening_data(tmp_path):
    features = project_features((observation(),))
    path = export_features(features,tmp_path,postgres_source_ref='postgres:fixture')
    obj = json.loads(path.read_text())
    obj['data_file'] = '../private.parquet'
    path.write_text(json.dumps(obj))
    with pytest.raises(ValueError):
        load_features(path)
