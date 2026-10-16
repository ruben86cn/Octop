import json
import unittest

from answer_contract import BackendAnswerError, TurnAnswer, is_model_failure
from task_queue import Queue
from test_repair import RepairTests


def state(*messages):
    return {"type": "state_snapshot", "data": {"messages": list(messages)}}


def user(text="hello"):
    return {"type": "human", "content": text}


def assistant(text, name="agent_A", **kwargs):
    return {"type": "ai", "content": text, "name": name, **kwargs}


class OwnerAnswerTests(unittest.TestCase):
    def test_real_peer_review_shape_selects_final_owner(self):
        turn = TurnAnswer("A", "hello")
        turn.observe(state(user()))
        turn.observe(
            {"type": "token", "content": "Source says delete the action. REVIEW_SOURCE_QUERY"}
        )
        turn.observe(state(user("review this"), assistant("Rejected", "agent_B")))
        turn.observe({"type": "tool_result", "messages": [{"type": "tool", "content": "Rejected"}]})
        turn.observe({"type": "token", "content": "Revision passed."})
        turn.observe(state(user(), assistant("", tool_calls=[{"name": "ask_agent"}])))
        turn.observe({"type": "token", "content": "01 first, then 02."})
        turn.observe(state(user(), assistant("01 first, then 02.")))
        self.assertEqual(
            turn.finish("thr_actual"), {"answer": "01 first, then 02.", "thread_id": "thr_actual"}
        )

    def test_historical_answer_and_peer_only_are_not_success(self):
        turn = TurnAnswer("A", "hello")
        turn.observe(state(user("old"), assistant("Old answer"), user()))
        turn.observe({"type": "token", "content": "Peer response"})
        turn.observe(state(user("review this"), assistant("Peer response", "agent_B")))
        with self.assertRaises(BackendAnswerError):
            turn.finish()

    def test_tool_call_not_a_final_answer(self):
        turn = TurnAnswer("A", "hello")
        turn.observe(state(user(), assistant("Will check", tool_calls=[{"name": "read_file"}])))
        with self.assertRaises(BackendAnswerError):
            turn.finish()

    def test_failure_after_partial_output_and_quoted_marker(self):
        turn = TurnAnswer("A", "hello")
        turn.observe({"type": "token", "content": "Let me check."})
        turn.observe(
            state(user(), assistant("[model_call_failed]\nThe model API request failed.", None))
        )
        with self.assertRaisesRegex(BackendAnswerError, "backend_model_failed"):
            turn.finish()
        self.assertFalse(is_model_failure("The marker `[model_call_failed]` signals failure."))

    def test_multimodal_text_content_preserved(self):
        turn = TurnAnswer("A", "hello")
        turn.observe(state(user(), assistant([{"type": "text", "text": "N=32; -20.05%"}])))
        self.assertEqual(turn.finish()["answer"], "N=32; -20.05%")

    def test_existing_attachment_path_hint_is_not_another_question(self):
        turn = TurnAnswer("A", "Read this file", attachments=True)
        turn.observe(
            state(
                user("Read this file\n\n[File: notes.txt (inbound/notes.txt)]"),
                assistant("The requested file answer."),
            )
        )
        self.assertEqual(turn.finish()["answer"], "The requested file answer.")


class QueueFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_and_legacy_failure_never_succeeded(self):
        calls = []

        async def backend(*args):
            calls.append(args)
            return {"answer": "[model_call_failed]\nThe model API request failed."}

        queue = Queue(":memory:", backend, {"A"})
        job = queue.submit("A", "hello", idem="one")
        await queue.worker
        self.assertEqual(queue.view(job["job_id"])["error_code"], "backend_model_failed")
        self.assertFalse(queue.view(job["job_id"])["retryable"])
        self.assertEqual(queue.submit("A", "hello", idem="one")["job_id"], job["job_id"])
        self.assertEqual(len(calls), 1)
        raw = json.dumps({"answer": "[model_call_failed]\nThe model API request failed."})
        queue.db.execute(
            "UPDATE jobs SET status='succeeded',result=?,error=NULL WHERE id=?",
            (raw, job["job_id"]),
        )
        queue.db.commit()
        self.assertEqual(queue.view(job["job_id"])["status"], "failed")
        self.assertIsNone(queue.view(job["job_id"])["result"])
        self.assertEqual(
            queue.db.execute("SELECT result FROM jobs WHERE id=?", (job["job_id"],)).fetchone()[0],
            raw,
        )
        queue.db.close()


class WireAnswerTests(RepairTests):
    async def test_wire_snapshot_wins_over_peer_tokens(self):
        result, _ = await self.backend_case(
            [
                state(user()),
                {"type": "token", "content": "Peer source. Rejected. Passed."},
                {"type": "tool_result"},
                state(user(), assistant("Verified final answer")),
                {"type": "done"},
            ]
        )
        self.assertEqual(result["result"]["answer"], "Verified final answer")

    async def test_error_code_and_failed_text(self):
        for frames in (
            [{"type": "error", "error_code": "model_call_failed", "message": "private"}],
            [
                {"type": "token", "content": "[model_call_failed]\nThe model API request failed."},
                {"type": "done"},
            ],
        ):
            result, _ = await self.backend_case(list(frames))
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error_code"], "backend_model_failed")
            self.assertIsNone(result["result"])

    async def test_localized_error_keeps_the_machine_capacity_code(self):
        result, _ = await self.backend_case(
            [{"type": "error", "error_code": "rate_limit", "message": "服务暂不可用"}]
        )
        self.assertEqual(result["error_code"], "backend_capacity_limited")

    async def test_hitl_required_is_not_empty_success(self):
        result, _ = await self.backend_case(
            [{"type": "hitl_required", "request": {"private": "details"}}]
        )
        self.assertEqual(result["error_code"], "hitl")
        self.assertIsNone(result["result"])


if __name__ == "__main__":
    unittest.main()
