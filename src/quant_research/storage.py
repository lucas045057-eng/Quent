"""Content-addressed Parquet exports with preserved source and coverage evidence."""
from dataclasses import fields
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from quant_features.core import DEFINITION_VERSION, FeatureObservationV1, project_features
from quant_phase9.canonical import canonical_bytes, canonical_sha256


def _write_once(path, raw):
    try:
        with path.open('xb') as out:
            out.write(raw)
    except FileExistsError:
        if path.read_bytes() != raw:
            raise ValueError('immutable export conflict')


def export_features(features, directory: Path, *, postgres_source_ref: str):
    features = tuple(features)
    if not 0 < len(features) <= 16384 or not postgres_source_ref.startswith('postgres:'):
        raise ValueError('bounded features and PostgreSQL source reference required')
    directory = Path(directory).resolve()
    directory.mkdir(parents=True,exist_ok=True)
    rows = []
    for feature in features:
        row = {field.name:getattr(feature.observation,field.name) for field in fields(FeatureObservationV1)}
        row['value'] = str(row['value']) if row['value'] is not None else None
        row['definition_digest'] = feature.definition_digest
        rows.append(row)
    table = pa.Table.from_pylist(rows)
    buffer = pa.BufferOutputStream()
    pq.write_table(table,buffer,compression='zstd',row_group_size=4096,version='2.6')
    raw = buffer.getvalue().to_pybytes()
    data_sha = hashlib.sha256(raw).hexdigest()
    data_name = data_sha+'.parquet'
    _write_once(directory/data_name,raw)
    coverage = {kind:sum(feature.pit_status == kind for feature in features)
        for kind in ('POINT_IN_TIME','PIT_UNVERIFIED','SYNTHETIC_FIXTURE')}
    manifest = {'schema':'QUANT_FEATURE_EXPORT_V1','definition_version':DEFINITION_VERSION,
        'postgres_source_ref':postgres_source_ref,'data_file':data_name,'data_sha256':data_sha,
        'records_digest':str(canonical_sha256(features)),'row_count':len(features),
        'coverage':coverage,'pyarrow_version':pa.__version__}
    digest = str(canonical_sha256(manifest))
    manifest['manifest_digest'] = digest
    path = directory/(digest+'.manifest.json')
    _write_once(path,canonical_bytes(manifest)+b'\n')
    return path


def load_features(manifest_path: Path):
    manifest_path = Path(manifest_path).resolve()
    if manifest_path.stat().st_size > 65536:
        raise ValueError('manifest byte bound exceeded')
    def unique(pairs):
        obj = {}
        for key,value in pairs:
            if key in obj:
                raise ValueError('duplicate manifest key')
            obj[key] = value
        return obj
    manifest = json.loads(manifest_path.read_text(),object_pairs_hook=unique)
    if set(manifest) != {'schema','definition_version','postgres_source_ref','data_file','data_sha256',
        'records_digest','row_count','coverage','pyarrow_version','manifest_digest'}:
        raise ValueError('unknown manifest fields')
    body = {key:value for key,value in manifest.items() if key != 'manifest_digest'}
    if str(canonical_sha256(body)) != manifest['manifest_digest']:
        raise ValueError('manifest digest mismatch')
    if manifest['schema'] != 'QUANT_FEATURE_EXPORT_V1' or manifest['definition_version'] != DEFINITION_VERSION:
        raise ValueError('unknown feature export version')
    name = manifest['data_file']
    if Path(name).name != name or not name.endswith('.parquet'):
        raise ValueError('unsafe export data path')
    path = (manifest_path.parent/name).resolve()
    if not path.is_relative_to(manifest_path.parent) or path.stat().st_size > 32*1024*1024:
        raise ValueError('export path or byte bound violation')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest['data_sha256']:
        raise ValueError('Parquet data digest mismatch')
    table = pq.read_table(pa.BufferReader(raw))
    if not 0 < table.num_rows <= 16384 or table.num_rows != manifest['row_count']:
        raise ValueError('Parquet row bound/count mismatch')
    observations = []
    definition_digests = []
    for row in table.to_pylist():
        definition_digests.append(row.pop('definition_digest'))
        row['value'] = Decimal(row['value']) if row['value'] is not None else None
        observations.append(FeatureObservationV1(**row))
    features = project_features(observations)
    if [feature.definition_digest for feature in features] != definition_digests:
        raise ValueError('definition digest mismatch')
    if str(canonical_sha256(features)) != manifest['records_digest']:
        raise ValueError('record digest mismatch')
    return features
