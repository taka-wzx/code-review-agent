import json
from pathlib import Path
import tempfile
import unittest

import anyio
from mcp.shared.memory import create_connected_server_and_client_session

from code_review_agent.mcp_server import create_mcp
from code_review_agent.service_core import JobStore, RepositoryRegistry, ReviewService

from tests.test_week7_service_core import DIFF, FakeRunner


def _structured(result):
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


class Week7McpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        repo = root / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        registry = RepositoryRegistry.from_json(
            json.dumps({"owner/repo": str(repo.resolve())})
        )
        self.runner = FakeRunner()
        self.service = ReviewService(registry, JobStore(root / "state"), runner=self.runner)
        self.mcp = create_mcp(self.service)

    def tearDown(self):
        self.service.shutdown()
        self.temp.cleanup()

    def test_official_client_lists_and_calls_complete_surface(self):
        async def exercise():
            async with create_connected_server_and_client_session(self.mcp) as session:
                tools = await session.list_tools()
                self.assertEqual(
                    {tool.name for tool in tools.tools},
                    {"review_diff", "review_pr", "get_review_status"},
                )
                templates = await session.list_resource_templates()
                uris = {str(item.uriTemplate) for item in templates.resourceTemplates}
                self.assertEqual(
                    uris,
                    {"crag://reviews/{review_id}", "crag://traces/{review_id}"},
                )
                prompts = await session.list_prompts()
                self.assertEqual([prompt.name for prompt in prompts.prompts], ["review_change"])

                submitted = await session.call_tool(
                    "review_diff", {"repository": "owner/repo", "diff": DIFF}
                )
                job_id = _structured(submitted)["review_id"]
                for _ in range(100):
                    status = _structured(
                        await session.call_tool("get_review_status", {"review_id": job_id})
                    )
                    if status["state"] in {"succeeded", "failed"}:
                        break
                    await anyio.sleep(0.01)
                self.assertEqual(status["state"], "succeeded")

                resource = await session.read_resource(f"crag://reviews/{job_id}")
                review = json.loads(resource.contents[0].text)
                self.assertEqual(review["review_id"], job_id)
                trace = await session.read_resource(f"crag://traces/{job_id}")
                self.assertIn("redacted", trace.contents[0].text)

                prompt = await session.get_prompt(
                    "review_change",
                    {"repository": "owner/repo", "change": "PR 8", "focus": "security"},
                )
                text = prompt.messages[0].content.text
                self.assertIn("owner/repo", text)
                self.assertIn("security", text)
                self.assertIn("Do not request", text)

                pr = _structured(
                    await session.call_tool(
                        "review_pr", {"repository": "owner/repo", "pull_request": "8"}
                    )
                )
                self.assertFalse(pr["duplicate"])

        anyio.run(exercise)

    def test_protocol_errors_do_not_start_jobs(self):
        async def exercise():
            async with create_connected_server_and_client_session(self.mcp) as session:
                result = await session.call_tool(
                    "review_diff", {"repository": "owner/repo", "diff": "not a diff"}
                )
                self.assertTrue(result.isError)
                self.assertNotIn("Traceback", result.content[0].text)

        anyio.run(exercise)
        self.assertEqual(self.runner.calls, [])


if __name__ == "__main__":
    unittest.main()
