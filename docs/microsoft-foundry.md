# Microsoft Foundry through Unity AI Gateway

Runbook for exposing an Azure AI Foundry (Azure OpenAI) deployment as a
Unity AI Gateway model provider service and model service, authenticated with a
Unity Catalog service credential so no API key is stored.

## 1. Azure prerequisites (outside the accelerator)

These are Azure and Unity Catalog changes. Apply them only with authorisation
for the target subscription and metastore.

1. **Deployment.** Deploy the model on the Foundry resource. GlobalStandard is
   pay-per-token; an idle deployment costs nothing.

   ```bash
   az cognitiveservices account deployment create \
     -g <RESOURCE_GROUP> -n <FOUNDRY_RESOURCE> \
     --deployment-name <DEPLOYMENT_NAME> --model-name <MODEL> \
     --model-version <VERSION> --model-format OpenAI \
     --sku-name GlobalStandard --sku-capacity <K_TPM>
   ```

2. **Identity.** Use an Azure Databricks access connector's managed identity.
   A dedicated connector limits blast radius; reusing a storage connector is
   acceptable for sandboxes.

3. **Role.** Grant that identity `Cognitive Services OpenAI User`, scoped to
   the Foundry resource only:

   ```bash
   az role assignment create \
     --assignee-object-id <CONNECTOR_PRINCIPAL_ID> \
     --assignee-principal-type ServicePrincipal \
     --role "Cognitive Services OpenAI User" \
     --scope <FOUNDRY_RESOURCE_ID>
   ```

4. **Service credential.** Create a UC service credential on the connector:

   ```bash
   databricks credentials create-credential --json '{
     "name": "<SERVICE_CREDENTIAL>", "purpose": "SERVICE",
     "azure_managed_identity": {"access_connector_id": "<CONNECTOR_RESOURCE_ID>"}}'
   ```

   The provider-service owner needs `ACCESS` on it. Callers do not.

5. **Schema.** Ensure the target schema exists and the operator has
   `USE CATALOG`, `USE SCHEMA`, `CREATE SERVICE` (and `CREATE CONNECTION` for
   inline credentials, which this accelerator rejects).

## 2. Declare and apply

Use the Foundry example in `config/example.yml` (`model_provider_services` plus a
model service with an `external_model_config` destination). Then:

```bash
python -m aigw_accelerator.cli plan   --config config/customer.yml
python -m aigw_accelerator.cli apply  --config config/customer.yml --confirm
python -m aigw_accelerator.cli verify --config config/customer.yml
```

`plan` and `verify` compare declared `config.targets`, routing destinations and
GA `rate_limits` against live state. Changes to an existing service are shown as
`UPDATE ... <mask paths>` and require `allow_service_config_replacement: true`;
apply PATCHes only the differing paths.

Use the canonical `https://adb-<id>.<n>.azuredatabricks.net` host. Account
metadata has no `workspace_url` for Azure workspaces, so identity validation
derives the host from `deployment_name`; vanity hosts fail that check and can
redirect (HTTP 303) on Gateway calls.

## 3. Reasoning models and tools

OpenAI reasoning models (gpt-5.x, gpt-6.x, o-series) reject function tools
combined with `reasoning_effort` on `/v1/chat/completions`:

> Function tools with reasoning_effort are not supported for <model> in
> /v1/chat/completions. To use function tools, use /v1/responses ...

This is an upstream limitation; AI Gateway does not translate Chat Completions to
Responses. Include `openai/v1/responses` in `native_api_types` on both the
provider target and the model-service destination, and have tool-calling agents
use the Responses API. `plan` warns when a reasoning-model target lacks it.

```python
from openai import OpenAI

client = OpenAI(base_url="https://<workspace>/ai-gateway/openai/v1", api_key=token)
resp = client.responses.create(
    model="<catalog>.<schema>.<model_service>",
    input=[{"role": "user", "content": "..."}],
    tools=[{"type": "function", "name": "...", "parameters": {...}}],
    reasoning={"effort": "medium"},
)
# Return function_call_output items with previous_response_id=resp.id (or replay
# prior output items) so reasoning context is preserved across tool turns.
```

## 4. Verification and propagation

After changing targets or routing, allow a few minutes before testing; updates
were observed to take 1–3 minutes to reach the Gateway data plane. Confirm with
a tool call through `/ai-gateway/openai/v1/responses` and through the
`Databricks-Model-Provider-Service` header path.

Callers need `EXECUTE` on the service plus `USE CATALOG` / `USE SCHEMA` on its
parents.
