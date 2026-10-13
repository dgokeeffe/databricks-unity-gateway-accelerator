# Small example: UC function as an agent tool

This example uses a Databricks Supervisor Agent with one Unity Catalog SQL function. It is illustrative: replace the identifiers, and run it only in a workspace where you have permission to create the function and agent. The `supervisor-agents` CLI surface is Beta and may vary by CLI version.

## 1. Create a simple function

Run in a SQL warehouse or notebook, replacing the catalog and schema:

```sql
CREATE FUNCTION main.demo.greet(name STRING)
RETURNS STRING
LANGUAGE SQL
DETERMINISTIC
COMMENT 'Returns a short greeting for a name.'
RETURN concat('Hello, ', name, '!');
```

## 2. Create the agent

```bash
databricks supervisor-agents create-supervisor-agent "Greeting Agent" \
  --description "Answers greetings by calling the greeting function" \
  --instructions "For requests to greet someone, use the greet_person tool and return its result."
```

Copy the returned agent resource name (for example, `supervisor-agents/<id>`) and the agent's service-principal identity. The service principal, not merely the person who created the agent, needs permission to call the function.

## 3. Grant the agent access and attach the function tool

As a SQL principal with grant authority, grant the agent identity access to the catalog/schema and function. Replace `<agent_sp>` with the service-principal name or identity accepted by your workspace:

```sql
GRANT USE CATALOG ON CATALOG main TO `<agent_sp>`;
GRANT USE SCHEMA ON SCHEMA main.demo TO `<agent_sp>`;
GRANT EXECUTE ON FUNCTION main.demo.greet TO `<agent_sp>`;
```

Attach the UC function to the agent:

```bash
databricks supervisor-agents create-tool supervisor-agents/<id> greet_person --json '{
  "tool_type": "uc_function",
  "description": "Creates a short greeting for a supplied name",
  "uc_function": {"name": "main.demo.greet"}
}'
```

Check the configured tool with:

```bash
databricks supervisor-agents list-tools supervisor-agents/<id>
```

Once the supervisor agent's serving endpoint is online, try asking it: `Greet Alex.` Endpoint readiness can take several minutes; check the endpoint returned by agent creation with `databricks serving-endpoints get <endpoint_name>`.

## Notes

- Keep the function narrow and deterministic where possible; the agent's tool description should clearly state when to use it.
- The agent identity needs the applicable `USE CATALOG`, `USE SCHEMA`, and `EXECUTE` privileges. Follow your workspace's identity and least-privilege practices.
- This example documents setup only; it does not create workspace resources or grant access automatically.
