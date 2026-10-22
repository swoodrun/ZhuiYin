"""Read-only reuse planning and additive task-template evaluation."""
import copy
import json

from engine import Config
from task_templates import validate_snapshot, evaluate_contract


PRODUCT_VERSION = 'product-workbench-v1'


def execution_config(raw):
    config = Config.from_dict(raw)
    snapshot = raw.get('template_snapshot')
    if snapshot is not None:
        snapshot = validate_snapshot(snapshot)
        applied = config.public()
        ignored = {'agent_mode'} | ({'max_requests', 'billing'} if config.agent_mode == 'rules' else set())
        changed = [key for key, value in snapshot['config'].items()
                   if key not in ignored and applied.get(key, {} if key == 'billing' else None) != value]
        if changed:
            raise ValueError('任务参数已与模板不同，请重新载入模板或将当前参数另存为模板后运行。')
        config.product_template_snapshot = snapshot
    return config


def public_execution_config(config):
    result = config.public()
    snapshot = getattr(config, 'product_template_snapshot', None)
    if snapshot is not None:
        result['template_snapshot'] = copy.deepcopy(snapshot)
    return result


def attach_template(report, config):
    """Add a separate contract result without changing the original judge."""
    snapshot = getattr(config, 'product_template_snapshot', None)
    if not isinstance(report, dict) or snapshot is None:
        return report
    result = dict(report)
    result['template_snapshot'] = copy.deepcopy(snapshot)
    result['product_workflow'] = PRODUCT_VERSION

    def decorate(trial):
        if not isinstance(trial, dict):
            return trial
        value = dict(trial)
        value['template_contract'] = evaluate_contract(trial, snapshot['contract'], config=config.public())
        return value

    for key in ('baseline', 'fixed'):
        if isinstance(result.get(key), dict):
            result[key] = decorate(result[key])
    if isinstance(result.get('samples'), list):
        result['samples'] = [decorate(item) for item in result['samples']]
    return result


def prepare_retest(job, selected_ids, candidates):
    """Generate an ordinary input file; never resume an old side effect."""
    if not isinstance(selected_ids, list) or not 1 <= len(selected_ids) <= 500:
        raise ValueError('请选择 1—500 个可重测任务。')
    if any(not isinstance(value, str) or len(value) > 300 for value in selected_ids):
        raise ValueError('重测选择格式无效。')
    if len(set(selected_ids)) != len(selected_ids):
        raise ValueError('重测任务不能重复选择。')
    available = {item['candidate_id']: item for item in candidates}
    scenarios, seen = [], set()
    for candidate_id in selected_ids:
        item = available.get(candidate_id)
        if not item or not item.get('eligible') or not isinstance(item.get('config'), dict):
            raise ValueError('所选记录缺少可重建参数，不能重测。')
        config = Config.from_dict({**item['config'], 'agent_mode': 'rules', 'model_config': {}}).public()
        config.pop('billing', None)
        # The current workbench supplies the model, policy and batch budget.
        for key in ('agent_mode', 'max_requests', 'prompt_policy'):
            config.pop(key, None)
        identity = json.dumps(config, ensure_ascii=False, sort_keys=True)
        if identity in seen:
            continue
        seen.add(identity)
        scenarios.append({'name': str(item.get('name') or item.get('label') or candidate_id)[:180], 'config': config})
    if not scenarios:
        raise ValueError('未选择可重测的业务任务。')
    # Intake allows up to twenty scenarios per individual JSON file.
    files = []
    for start in range(0, len(scenarios), 20):
        files.append({'filename': f'历史重测-{start // 20 + 1:02d}.json',
                      'content': json.dumps({'kind': 'scenarios', 'scenarios': scenarios[start:start+20]}, ensure_ascii=False)})
    return files, {'source_run_id': job.get('run_id'), 'selected_records': len(selected_ids),
                   'unique_tasks': len(scenarios), 'scope': '在全新业务环境中重测；模型、指令及整批预算沿用当前工作台。'}
