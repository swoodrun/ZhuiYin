"""Source-only portable regression entry. No research data or external model calls."""
from pathlib import Path
import json,os,sys,tempfile,time,unittest
ROOT=Path(__file__).resolve().parent
temporary=ROOT.parent/'离线测试临时目录'
temporary.mkdir(exist_ok=True)
tempfile.tempdir=str(temporary)
os.environ['PYTHONDONTWRITEBYTECODE']='1'
modules=['test_advanced_engine', 'test_agent_suite', 'test_agent_workbench', 'test_audit_metadata', 'test_billing_integration', 'test_billing_job_adapters', 'test_campaign_integration', 'test_concurrency_lab', 'test_credentials', 'test_deepseek_compat', 'test_desktop_browser', 'test_desktop_shell', 'test_desktop', 'test_eight_arm_launcher', 'test_eight_arm_package_links', 'test_engine', 'test_evaluation_protocol_v2', 'test_evaluation_summary_v2', 'test_evidence_boundary', 'test_evidence_qualification', 'test_evidence_verifier_stops', 'test_experiment_budget', 'test_frontend_outcomes', 'test_import_static_judge', 'test_incomplete_baseline_storage', 'test_job_transport', 'test_model_budget', 'test_paired_evidence', 'test_payload_import_contract', 'test_persistence', 'test_plain_copy', 'test_protocol_import_v2', 'test_release_guards', 'test_report_roundtrip', 'test_run_billing', 'test_server_upgrade', 'test_structured_credentials', 'test_study_trial_observer', 'test_study_v2', 'test_study', 'test_test_suite_server', 'test_test_suite', 'test_uploads', 'test_worker_privacy', 'test_diagnostics', 'test_experiment_compare', 'test_task_templates', 'test_product_apis', 'test_product_edge_cases', 'test_product_integration', 'test_retest_templates']
started=time.time()
suite=unittest.defaultTestLoader.loadTestsFromNames(modules)
result=unittest.TextTestRunner(verbosity=1).run(suite)
summary={'modules':len(modules),'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'passed':result.wasSuccessful(),'seconds':round(time.time()-started,3),'external_model_requests':0,'python':sys.version,'scope':'source-only portable subset using synthetic data and loopback; not the historical workspace full suite or real-model effectiveness'}
(ROOT.parent/'离线测试结果.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
sys.exit(not result.wasSuccessful())
