# Retry history replay fix

A failed model/tool turn can leave tool calls without results. On the next run,
DeepAgents' history repair node returns the previous messages plus repair tool
results. With redacted trace inputs, LangGraph's message callback cannot seed its
seen-message IDs and can publish the old assistant messages on its messages
stream. Harness 1.0.5 projects these as new tokens without message IDs.

The fix uses checkpoint IDs from the values stream to suppress replay before
text projection, with state scoped to each stream's ThinkSplitter. New model
messages retain their IDs, including partial attempts; equal text with a new ID
is not suppressed. The existing HITL projection wrapper remains composable.

TurnHistoryTracker stores identified partial messages separately and defers to
checkpoint messages with the same ID. Legacy multi-message token streams that
are already covered by finalized assistant messages no longer create an extra
concatenated reply. Unfinished output and error records are still preserved.

Regression coverage uses a real LangGraph repair node with redacted inputs,
streaming delta/final-message deduplication, separate invocation state, identical
new/old wording, partial retries, and multi-step history persistence. The raw
reproducer on the installed production dependencies emitted OLD then NEW before
the fix, and only NEW with the fix enabled.

This change does not rewrite existing conversations or checkpoints, change the
retry button's semantics, rerun tools, or change model retry policy.
