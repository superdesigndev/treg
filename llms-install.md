# Connect Treg to Cline

Treg runs a hosted MCP server at `https://treg.to/mcp/`. It uses Streamable HTTP. In Cline, add a remote server named `treg` at that URL. For browser authentication, choose **Authorize** and sign in to the Treg team you want to use.

For a noninteractive installation, ask the user to supply a Treg team key privately as `TREG_TOKEN` in the terminal environment. They can obtain a key from **Team → API Keys** in the [Treg dashboard](https://treg.to/app). Run this from the Cline CLI environment:

```sh
cline mcp add treg https://treg.to/mcp/ --transport http \
  --header "Authorization: Bearer ${TREG_TOKEN:?Set your Treg team key first}" --yes
```

Start a new Cline task so the server’s tools are available. Ask Cline to use `catalog_get` with `{"endpoint_id":"treg.web.search"}` and show the method, inputs and pricing currency. Expect `POST`, an input named `q`, and prices in USD. This reads catalog metadata and is free. Calling a provider is a separate step.

[Treg MCP reference](https://treg.to/llms.txt) · [Endpoint record](https://treg.to/catalog/endpoints/treg.web.search)
