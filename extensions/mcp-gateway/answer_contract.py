"""Select the completed owner message, never concatenated peer/tool streams."""


class BackendAnswerError(RuntimeError):
    def __init__(self, code, thread_id=None):
        super().__init__(code)
        self.code = code
        self.thread_id = thread_id


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            block
            if isinstance(block, str)
            else str(block.get("text") or "")
            if isinstance(block, dict) and block.get("type") == "text"
            else ""
            for block in value
        )
    return ""


def is_model_failure(answer):
    # The installed harness documents this exact leading failure marker.
    # Merely discussing/quoting the marker in an otherwise real answer is fine.
    return isinstance(answer, str) and answer.lstrip().startswith("[model_call_failed]")


def validate_answer(result):
    answer = result.get("answer") if isinstance(result, dict) else None
    if not isinstance(answer, str) or not answer.strip():
        raise BackendAnswerError(
            "backend_empty_response", result.get("thread_id") if isinstance(result, dict) else None
        )
    if is_model_failure(answer):
        raise BackendAnswerError("backend_model_failed", result.get("thread_id"))
    return answer


class TurnAnswer:
    def __init__(self, agent, question, attachments=False):
        self.agent = agent
        self.question = question.strip()
        self.attachments = attachments
        self.seen_state = False
        self.seen_tools = False
        self.owner_state = False
        self.answer = ""
        self.pieces = []
        self.characters = 0

    def observe(self, frame):
        kind = frame.get("type")
        if kind in ("tool_call_chunk", "tool_result"):
            self.seen_tools = True
        if kind in ("token", "delta", "text_delta"):
            value = frame.get("text") or frame.get("content") or frame.get("delta")
            if isinstance(value, str):
                self.pieces.append(value)
                self.characters += len(value)
            if self.characters > 200000:
                raise BackendAnswerError("backend_output_limit")
        if kind != "state_snapshot":
            return
        self.seen_state = True
        data = frame.get("data")
        messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list):
            return
        # model_dump() is the documented websocket serializer. Also accept the
        # type/data wrapper used by history exports, without reading the DB.
        messages = [m.get("data", m) if isinstance(m, dict) else {} for m in messages]
        users = [
            i for i, m in enumerate(messages) if m.get("type", m.get("role")) in ("human", "user")
        ]
        if not users:
            return
        request_text = text_content(messages[users[-1]].get("content")).strip()
        matches = request_text == self.question or (
            self.attachments and request_text.startswith(self.question + "\n\n")
        )
        if not matches:
            return  # A peer task or historical replay cannot supply this answer.
        self.owner_state = True
        self.answer = ""
        current = messages[users[-1] + 1 :]
        if not current:
            return
        last = current[-1]
        if last.get("type", last.get("role")) not in ("ai", "assistant"):
            return
        if last.get("name") not in (None, "", "agent_" + self.agent):
            return
        if last.get("tool_calls") or last.get("invalid_tool_calls"):
            return
        self.answer = text_content(last.get("content")).strip()
        if len(self.answer) > 200000:
            raise BackendAnswerError("backend_output_limit")

    def finish(self, thread_id=None):
        if self.seen_state:
            if not self.owner_state:
                raise BackendAnswerError("backend_missing_final_response", thread_id)
            answer = self.answer
        elif self.seen_tools:
            # Legacy token-only transports do not identify the final owner
            # after delegation. Fail explicitly rather than return peer drafts.
            raise BackendAnswerError("backend_missing_final_response", thread_id)
        else:
            answer = "".join(self.pieces).strip()
        result = {"answer": answer, "thread_id": thread_id}
        validate_answer(result)
        return result
