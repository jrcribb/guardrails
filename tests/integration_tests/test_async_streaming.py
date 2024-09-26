# 3 tests
# 1. Test streaming with OpenAICallable (mock openai.Completion.create)
# 2. Test streaming with OpenAIChatCallable (mock openai.ChatCompletion.create)
# 3. Test string schema streaming
# Using the LowerCase Validator, and a custom validator to show new streaming behavior
from typing import Any, Callable, Dict, List, Optional, Union

import asyncio
import pytest
from pydantic import BaseModel, Field

import guardrails as gd
from guardrails.utils.casting_utils import to_int
from guardrails.validator_base import (
    ErrorSpan,
    FailResult,
    OnFailAction,
    PassResult,
    ValidationResult,
    Validator,
    register_validator,
)
from tests.integration_tests.test_assets.validators import LowerCase, MockDetectPII

expected_raw_output = {"statement": "I am DOING well, and I HOPE you aRe too."}
expected_fix_output = {"statement": "i am doing well, and i hope you are too."}
expected_noop_output = {"statement": "I am DOING well, and I HOPE you aRe too."}
expected_filter_refrain_output = {}


@register_validator(name="minsentencelength", data_type=["string", "list"])
class MinSentenceLengthValidator(Validator):
    def __init__(
        self,
        min: Optional[int] = None,
        max: Optional[int] = None,
        on_fail: Optional[Callable] = None,
    ):
        super().__init__(
            on_fail=on_fail,
            min=min,
            max=max,
        )
        self._min = to_int(min)
        self._max = to_int(max)

    def sentence_split(self, value):
        return list(map(lambda x: x + ".", value.split(".")[:-1]))

    def validate(self, value: Union[str, List], metadata: Dict) -> ValidationResult:
        sentences = self.sentence_split(value)
        error_spans = []
        index = 0
        for sentence in sentences:
            if len(sentence) < self._min:
                error_spans.append(
                    ErrorSpan(
                        start=index,
                        end=index + len(sentence),
                        reason=f"Sentence has length less than {self._min}. "
                        f"Please return a longer output, "
                        f"that is shorter than {self._max} characters.",
                    )
                )
            if len(sentence) > self._max:
                error_spans.append(
                    ErrorSpan(
                        start=index,
                        end=index + len(sentence),
                        reason=f"Sentence has length greater than {self._max}. "
                        f"Please return a shorter output, "
                        f"that is shorter than {self._max} characters.",
                    )
                )
            index = index + len(sentence)
        if len(error_spans) > 0:
            return FailResult(
                validated_chunk=value,
                error_spans=error_spans,
                error_message=f"Sentence has length less than {self._min}. "
                f"Please return a longer output, "
                f"that is shorter than {self._max} characters.",
            )
        return PassResult(validated_chunk=value)

    def validate_stream(self, chunk: Any, metadata: Dict, **kwargs) -> ValidationResult:
        return super().validate_stream(chunk, metadata, **kwargs)


class Delta:
    content: str

    def __init__(self, content):
        self.content = content


class Choice:
    text: str
    finish_reason: str
    index: int
    delta: Delta

    def __init__(self, text, delta, finish_reason, index=0):
        self.index = index
        self.delta = delta
        self.text = text
        self.finish_reason = finish_reason


class MockOpenAIV1ChunkResponse:
    choices: list
    model: str

    def __init__(self, choices, model):
        self.choices = choices
        self.model = model


class Response:
    def __init__(self, chunks):
        self.chunks = chunks

        async def gen():
            for chunk in self.chunks:
                yield MockOpenAIV1ChunkResponse(
                    choices=[
                        Choice(
                            delta=Delta(content=chunk),
                            text=chunk,
                            finish_reason=None,
                        )
                    ],
                    model="OpenAI model name",
                )
            await asyncio.sleep(0)  # Yield control to the event loop

        self.completion_stream = gen()


class LowerCaseFix(BaseModel):
    statement: str = Field(
        description="Validates whether the text is in lower case.",
        validators=[LowerCase(on_fail=OnFailAction.FIX)],
    )


class LowerCaseNoop(BaseModel):
    statement: str = Field(
        description="Validates whether the text is in lower case.",
        validators=[LowerCase(on_fail=OnFailAction.NOOP)],
    )


class LowerCaseFilter(BaseModel):
    statement: str = Field(
        description="Validates whether the text is in lower case.",
        validators=[LowerCase(on_fail=OnFailAction.FILTER)],
    )


class LowerCaseRefrain(BaseModel):
    statement: str = Field(
        description="Validates whether the text is in lower case.",
        validators=[LowerCase(on_fail=OnFailAction.REFRAIN)],
    )


expected_minsentence_noop_output = ""


class MinSentenceLengthNoOp(BaseModel):
    statement: str = Field(
        description="Validates whether the text is in lower case.",
        validators=[MinSentenceLengthValidator(on_fail=OnFailAction.NOOP)],
    )


STR_PROMPT = "Say something nice to me."

PROMPT = """
Say something nice to me.

${gr.complete_json_suffix}
"""

POETRY_CHUNKS = [
    '"John, under ',
    "GOLDEN bridges",
    ", roams,\n",
    "SAN Francisco's ",
    "hills, his HOME.\n",
    "Dreams of",
    " FOG, and salty AIR,\n",
    "In his HEART",
    ", he's always THERE.",
]


@pytest.mark.asyncio
async def test_filter_behavior(mocker):
    mocker.patch(
        "litellm.acompletion",
        return_value=Response(POETRY_CHUNKS),
    )

    guard = gd.AsyncGuard().use_many(
        MockDetectPII(
            on_fail=OnFailAction.FIX,
            pii_entities="pii",
            replace_map={"John": "<PERSON>", "SAN Francisco's": "<LOCATION>"},
        ),
        LowerCase(on_fail=OnFailAction.FILTER),
    )
    prompt = """Write me a 4 line poem about John in San Francisco. 
    Make every third word all caps."""
    gen = await guard(
        model="gpt-3.5-turbo",
        max_tokens=10,
        temperature=0,
        stream=True,
        prompt=prompt,
    )

    text = ""
    final_res = None
    async for res in gen:
        final_res = res
        text = text + res.validated_output

    assert final_res.raw_llm_output == ", he's always THERE."
    assert text == ""
