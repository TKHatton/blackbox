"""A scripted stand-in for Gemini, used by the Phase 2 tests.

The tests need to prove that the Flight Recorder captures reasoning, tool calls,
and tool results in the right causal shape. That is a property of the recorder,
not of the model, so the model is scripted here and the tests stay deterministic
and free of network calls.

This is a test double. It never ships: nothing under ``blackbox/`` imports it,
and all real inference goes through Gemini on Vertex AI.
"""

from typing import Any, AsyncGenerator, Dict, List, Optional

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types


def say(text: str) -> types.Content:
    """A turn where the model only talks."""
    return types.Content(role="model", parts=[types.Part(text=text)])


def think_and_call(
    reasoning: str, tool_name: str, args: Dict[str, Any]
) -> types.Content:
    """A turn where the model states its reasoning and then calls a tool."""
    return types.Content(
        role="model",
        parts=[
            types.Part(text=reasoning),
            types.Part(
                function_call=types.FunctionCall(name=tool_name, args=dict(args))
            ),
        ],
    )


class ScriptedLlm(BaseLlm):
    """Replays a fixed list of turns, one per call.

    Raises if the script runs out, so a test that changes the agent's behaviour
    fails loudly instead of hanging or quietly producing an empty turn.
    """

    script: List[types.Content] = []
    calls: List[Any] = []

    def __init__(self, script: List[types.Content], model: str = "scripted-test-model"):
        super().__init__(model=model)
        # Pydantic models on BaseLlm need assignment through object.__setattr__ only
        # if frozen; ADK's BaseLlm is a normal mutable model, so plain assignment works.
        self.script = list(script)
        self.calls = []

    @staticmethod
    def supported_models() -> List[str]:
        return ["scripted-test-model"]

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self.calls.append(llm_request)
        if not self.script:
            raise AssertionError(
                "ScriptedLlm ran out of turns. The agent made more model calls than "
                "the test scripted."
            )
        content = self.script.pop(0)
        yield LlmResponse(content=content)
