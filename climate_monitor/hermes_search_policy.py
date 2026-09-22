"""Static acquisition plugin, copied before snapshot publication."""
import json
from climate_monitor.hermes_attempt_policy import verified_binding
from climate_monitor.hermes_acquisition_hooks import transform_search_tool_result
from climate_monitor.request_budget import candidate_handle_protocol

stage_schema = {'name': 'climate_stage_candidate', 'description': 'Resolve one trusted search result handle, apply the frozen date policy, and conditionally obtain its governed article body.', 'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {'result_handle': {'type': 'string'}, 'source_key': {'type': 'string'}}, 'required': ['result_handle', 'source_key']}}

finalize_schema = {'name': 'climate_finalize_candidate', 'description': 'Attach bounded relevance annotations to one staged candidate receipt.', 'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {'candidate_handle': {'type': 'string'}, 'selected': {'type': 'boolean'}, 'title': {'type': 'string', 'maxLength': 500}, 'summary': {'type': 'string', 'maxLength': 4000}, 'selection_reason': {'type': 'string', 'maxLength': 2000}}, 'required': ['candidate_handle', 'selected', 'title', 'summary', 'selection_reason']}}


def register(ctx):
    path, binding = verified_binding()
    def transform(**kwargs):
        current, _ = verified_binding()
        return transform_search_tool_result(current, **kwargs)
    ctx.register_hook("transform_tool_result", transform)
    if candidate_handle_protocol(binding):
        from scripts.run_agent_acquisition import _stage_candidate_receipt, _finalize_candidate_receipt
        def stage(args, session_id=None, **_kwargs):
            current, _ = verified_binding()
            return json.dumps(_stage_candidate_receipt(current, session_id=session_id, **args), sort_keys=True, separators=(',', ':'))
        def finalize(args, session_id=None, **_kwargs):
            current, _ = verified_binding()
            return json.dumps(_finalize_candidate_receipt(current, session_id=session_id, **args), sort_keys=True, separators=(',', ':'))
        ctx.register_tool(name='climate_stage_candidate', toolset='climate_acquisition', schema=stage_schema, handler=stage)
        ctx.register_tool(name='climate_finalize_candidate', toolset='climate_acquisition', schema=finalize_schema, handler=finalize)
