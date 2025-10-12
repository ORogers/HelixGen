from __future__ import annotations

import json

from hlxgen.dataset import ModelCatalog
from hlxgen.llm import _CHAIN_SCHEMA, _compose_prompt


def test_compose_prompt_includes_schema(dataset_path):
    catalog = ModelCatalog(dataset_path)
    prompt = _compose_prompt("Create a bluesy crunch", catalog)
    schema_json = json.dumps(_CHAIN_SCHEMA, indent=2)

    assert "Required JSON schema:" in prompt
    assert schema_json in prompt
