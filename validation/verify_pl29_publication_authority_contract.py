import json, sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
checks=[]
def ck(n,c,d=''):
    checks.append({'name':n,'ok':bool(c),'detail':d})
trainer=(root/'backend/tools/train_nse_smart_model.py').read_text()
pub=(root/'backend/core/ai_training_publication_service.py').read_text()
app=(root/'backend/application_runtime.py').read_text()
config=(root/'backend/config.py').read_text()
front=json.loads((root/'frontend/release-identity.json').read_text())
index=(root/'frontend/index.html').read_text()
ck('trainer canonical authority','TRAINING_DATA_AUTHORITY = "PARQUET_DUCKDB"' in trainer and '"training_data_source": TRAINING_DATA_AUTHORITY' in trainer)
ck('pipeline lineage separated','TRAINING_PIPELINE_SOURCE = "R46_MATERIALIZED_RESEARCH_PANEL_TO_INCREMENTAL_FEATURE_STORE"' in trainer and '"training_pipeline_source": data_source' in trainer)
ck('legacy PL28 token exact mapped','LEGACY_MATERIALIZED_PIPELINE_SOURCE = "R46_MATERIALIZED_RESEARCH_PANEL_TO_INCREMENTAL_FEATURE_STORE"' in pub and 'raw_source == LEGACY_MATERIALIZED_PIPELINE_SOURCE' in pub)
ck('unknown authority fail closed','unsupported training data source authority' in pub)
ck('startup outbox recovery preserved','publish_pending(' in app and 'publication_outbox' in app)
# Functional static normaliser proof without constructing store
sys.path.insert(0,str(root/'backend'))
from core.ai_training_publication_service import AITrainingPublicationService
base={'model_id':'m','model_version':'1','framework':'HistGradientBoosting','horizon_days':10,'feature_manifest_hash':'f','dataset_fingerprint':'d','trained_through':'2026-08-06','lifecycle_state':'SHADOW'}
a=AITrainingPublicationService._normalise_model({**base,'training_data_source':'R46_MATERIALIZED_RESEARCH_PANEL_TO_INCREMENTAL_FEATURE_STORE'})
ck('legacy maps to canonical',a.get('training_data_source')=='PARQUET_DUCKDB' and a.get('training_pipeline_source')=='R46_MATERIALIZED_RESEARCH_PANEL_TO_INCREMENTAL_FEATURE_STORE',str(a))
b=AITrainingPublicationService._normalise_model({**base,'training_data_source':'PARQUET_DUCKDB','training_pipeline_source':'R46_MATERIALIZED_RESEARCH_PANEL_TO_INCREMENTAL_FEATURE_STORE'})
ck('canonical remains canonical',b.get('training_data_source')=='PARQUET_DUCKDB')
try:
    AITrainingPublicationService._normalise_model({**base,'training_data_source':'UNTRUSTED_SOURCE'})
    rejected=False
except ValueError:
    rejected=True
ck('unknown source rejected',rejected)
marker='production-usability-r8-pl29-publication-authority-8086'
ck('PL29 marker',marker in config and front.get('build_marker')==marker and f'data-build-marker="{marker}"' in index)
ck('broker authority frozen','broker_authority="NONE"' in pub or 'broker_authority="NONE"' in trainer)
failed=[x for x in checks if not x['ok']]
print(json.dumps({'contract':'PL29_PUBLICATION_AUTHORITY_CONTRACT_CLOSURE','ok':not failed,'passed':len(checks)-len(failed),'failed':len(failed),'checks':checks},indent=2,default=str))
raise SystemExit(1 if failed else 0)
