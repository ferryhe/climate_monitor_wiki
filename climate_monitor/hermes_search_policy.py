"""Static acquisition plugin, copied before snapshot publication."""
import json
import os
from pathlib import Path
import subprocess
from climate_monitor.hermes_attempt_policy import verified_binding
from climate_monitor.hermes_acquisition_hooks import transform_search_tool_result
from climate_monitor.request_budget import RequestBudget, candidate_handle_protocol, ledger_path

stage_schema = {'name': 'climate_stage_candidate', 'description': 'Resolve one trusted search result handle, apply the frozen date policy, and conditionally obtain its governed article body.', 'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {'result_handle': {'type': 'string'}, 'source_key': {'type': 'string'}}, 'required': ['result_handle', 'source_key']}}

finalize_schema = {'name': 'climate_finalize_candidate', 'description': 'Attach bounded relevance annotations to one staged candidate receipt.', 'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {'candidate_handle': {'type': 'string'}, 'selected': {'type': 'boolean'}, 'title': {'type': 'string', 'maxLength': 500}, 'summary': {'type': 'string', 'maxLength': 4000}, 'selection_reason': {'type': 'string', 'maxLength': 2000}}, 'required': ['candidate_handle', 'selected', 'title', 'summary', 'selection_reason']}}


def register(ctx):
    path, binding = verified_binding()
    def transform(**kwargs):
        current, _ = verified_binding()
        return transform_search_tool_result(current, **kwargs)
    ctx.register_hook("transform_tool_result", transform)
    if candidate_handle_protocol(binding):
        from scripts.run_agent_acquisition import _finalize_candidate_receipt
        def stage(args, session_id=None, **_kwargs):
            _, current_binding = verified_binding()
            RequestBudget(ledger_path(current_binding), current_binding, initialize=False).result_handle(
                args.get('result_handle'), session_id=session_id)
            root = Path(os.environ['HERMES_HOME']).parent
            payload = json.loads((root / 'manifest.json').read_text())
            result = subprocess.run([payload['acquisition_runtime']['interpreter'], '-I', '-S',
                str(root / 'bootstrap/launcher.py'), '--candidate-stage'],
                input=json.dumps({**args, 'session_id': session_id}), text=True, capture_output=True)
            if result.returncode:
                raise ValueError('frozen candidate stage failed')
            return json.dumps(json.loads(result.stdout), sort_keys=True, separators=(',', ':'))
        def finalize(args, session_id=None, **_kwargs):
            current, _ = verified_binding()
            return json.dumps(_finalize_candidate_receipt(current, session_id=session_id, **args), sort_keys=True, separators=(',', ':'))
        ctx.register_tool(name='climate_stage_candidate', toolset='climate_acquisition', schema=stage_schema, handler=stage)
        ctx.register_tool(name='climate_finalize_candidate', toolset='climate_acquisition', schema=finalize_schema, handler=finalize)
