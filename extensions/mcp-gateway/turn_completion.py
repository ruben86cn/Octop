"""Confirm the owning dashboard turn is idle before treating done as final."""


class TurnCompletion:
    def __init__(self, thread_id=None):
        self.thread_id = thread_id or None
        self.saw_done = False

    def observe(self, frame):
        thread = frame.get("thread_id")
        if isinstance(thread, str) and thread:
            if self.thread_id and thread != self.thread_id:
                return "ignore"
            self.thread_id = thread
        kind = frame.get("type")
        if kind == "done":
            self.saw_done = True
            # Legacy transports without a thread have no subscription status.
            # TurnAnswer still requires a valid unambiguous owner answer.
            return "probe" if self.thread_id else "finish"
        if (
            kind == "turn_status"
            and self.saw_done
            and self.thread_id
            and thread == self.thread_id
            and frame.get("active") is False
        ):
            return "finish"
        return "wait"

    def status_request(self):
        if self.saw_done and self.thread_id:
            return {"type": "subscribe", "thread_id": self.thread_id}
        return None
