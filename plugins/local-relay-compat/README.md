# Local relay compatibility (optional)

Install this directory using Octop's existing local plugin workflow, then enable
it and reload the relevant experts. It is not automatically enabled by this fork.

For ChatOpenAI models at exactly `http://127.0.0.1:18080/v1`, the plugin uses a
600-second maximum gap between parsed streaming chunks. It copies the request's
model instead of changing the cached model or patching `octop-harness`.

B8 explicitly selects Chat Completions by default. If protocol selection is still
unset, this plugin pins Chat Completions; explicit Responses and native tool search
remain intact. Other endpoints are untouched. Change the constant in a reviewed
commit if your local relay uses another endpoint.

This hook applies to conversational model calls. B8 memory extraction uses its
own request timeout/retry settings; configure those in Memory advanced settings.
The MCP gateway also has an independent overall task timeout.
