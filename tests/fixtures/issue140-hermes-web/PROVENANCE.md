# Docker-pinned Hermes web helper provenance

Commit: `5538bd1f933be2e94aca9755deca5cc59cccc553` (Dockerfile pin, CLI 0.20.5).
Upstream source: `NousResearch/hermes-agent` at that exact commit.
The JSON contains exact function source segments only; no functions are executed.
Python 3.12 ASTs differ solely by empty `type_params` fields from already allowed Python 3.11 ASTs.

| Helper | Existing matching source | Python 3.11 AST SHA256 | Python 3.12 AST SHA256 |
|---|---|---|---|
| tools/xai_http.py:get_env_value | fcbd1076 | ab2e4e36abde4ae43a993797b64396e20041e5723813b19ef7f528c4a4282156 | dc5f210a69fed26b9217594e0b54e9f0c4753bbfa072a62d7d813b270e2153a1 |
| agent/web_search_provider.py:get_provider_env | fcbd1076 | cd44de6c536d89d7fa6edc57a78329c2194175e08199a4cbc8c312cc6b29e5bf | 5f85f8a3beea7a24126b872337409a60ce7e2c36b6f20c462a39aeefd3b9d706 |
| tools/managed_tool_gateway.py:build_vendor_gateway_url | fcbd1076 | da2a71ff0681e017cf3d2ee99f8744f7aa69834819cf422ac6685603c39a0403 | 6246e435806165a3d22dd5438c7444ed5bf206d16de1ddc250bca1e0e3b29bf2 |
| tools/web_tools.py:_env_value | fcbd1076 | abb1cab77267e3e9262c4765331ab534ffb7da34316061ba51a0d6786b9ecaed | f4c76d716dd6b3280105c9dbaf8013e59fd6074e7853903ee24fbb7bc8cb89a7 |
| tools/web_tools.py:_has_env | fcbd1076, 1ad89ac0 | 1c363b932da8e3a3e68b13f6f55b687902c7b62442b51839a528debfeece2f25 | d2cf256b7050751854d58ac70a2fa1d89ba09601427a9692e369ea94e67b3557 |
| tools/web_tools.py:_rescue_eligible | fcbd1076 | de395c54eb835046d76c9bc9d27c30e22c1c4449f6a653229bbff06cc6bc9d20 | d29371a1144b098e189e8da8b4a70db602ee6a3756f5d7547847b6619f4a644a |

Source-segment SHA256 values are in the JSON fixture. Six helpers forward literal/profile names, wrap config/process env lookup, derive a vendor gateway name, or select from the fixed rescue key map. No new environment-expression semantics are accepted. Source file membership and bytes remain independently snapshot-bound.
