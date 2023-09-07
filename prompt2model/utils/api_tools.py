"""Tools for accessing API-based models."""

from __future__ import annotations  # noqa FI58

import asyncio
import json
import logging
import time

import aiolimiter
import openai
import openai.error
import tiktoken
from aiohttp import ClientSession
from litellm import acompletion, completion
from tqdm.asyncio import tqdm_asyncio

# Note that litellm converts all API errors into openai errors,
# so openai errors are valid even when using other services.
API_ERRORS = (
    openai.error.APIError,
    openai.error.Timeout,
    openai.error.RateLimitError,
    openai.error.ServiceUnavailableError,
    openai.error.InvalidRequestError,
    json.decoder.JSONDecodeError,
    AssertionError,
)

ERROR_ERRORS_TO_MESSAGES = {
    openai.error.InvalidRequestError: "API Invalid Request: Prompt was filtered",
    openai.error.RateLimitError: "API rate limit exceeded. Sleeping for 10 seconds.",
    openai.error.APIConnectionError: "Error Communicating with API",
    openai.error.Timeout: "API Timeout Error: API Timeout",
    openai.error.ServiceUnavailableError: "API service unavailable error: {e}",
    openai.error.APIError: "API error: {e}",
}


class APIAgent:
    """A class for accessing API-based models."""

    def __init__(self, model_name: str = "gpt-3.5-turbo"):
        """Initialize APIAgent with an API key.

        Args:
            model_name: Name fo the model to use (by default, gpt-3.5-turbo).
        """
        self.model_name = model_name

    def generate_one_completion(
        self,
        prompt: str,
        temperature: float = 0,
        presence_penalty: float = 0,
        frequency_penalty: float = 0,
    ) -> openai.Completion:
        """Generate a chat completion using an API-based model.

        Args:
            prompt: A prompt asking for a response.
            temperature: What sampling temperature to use, between 0 and 2. Higher
                values like 0.8 will make the output more random, while lower values
                like 0.2 will make it more focused and deterministic.
            presence_penalty: Float between -2.0 and 2.0. Positive values penalize new
                tokens based on whether they appear in the text so far, increasing the
                model's likelihood to talk about new topics.
            frequency_penalty: Float between -2.0 and 2.0. Positive values penalize new
                tokens based on their existing frequency in the text so far, decreasing
                the model's likelihood of repeating the same line verbatim.

        Returns:
            A response object.
        """
        response = completion(  # completion gets the key from os.getenv
            model=self.model_name,
            messages=[
                {"role": "user", "content": f"{prompt}"},
            ],
            temperature=temperature,
            presence_penalty=presence_penalty,
            frequency_penalty=frequency_penalty,
        )
        return response

    async def generate_batch_completion(
        self,
        prompts: list[str],
        temperature: float = 1,
        responses_per_request: int = 5,
        requests_per_minute: int = 80,
    ) -> list[openai.Completion]:
        """Generate a batch responses from OpenAI Chat Completion API.

        Args:
            prompts: List of prompts to generate from.
            model_config: Model configuration.
            temperature: Temperature to use.
            responses_per_request: Number of responses for each request.
                i.e. the parameter n of API call.
            requests_per_minute: Number of requests per minute to allow.

        Returns:
            List of generated responses.
        """
        openai.aiosession.set(ClientSession())
        limiter = aiolimiter.AsyncLimiter(requests_per_minute)

        async def _throttled_completion_acreate(
            model: str,
            messages: list[dict[str, str]],
            temperature: float,
            max_tokens: int,
            n: int,
            top_p: float,
            limiter: aiolimiter.AsyncLimiter,
        ):
            async with limiter:
                for _ in range(3):
                    try:
                        return await acompletion(
                            model=model,
                            messages=messages,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            n=n,
                            top_p=top_p,
                        )
                    except tuple(ERROR_ERRORS_TO_MESSAGES.keys()) as e:
                        if isinstance(
                            e,
                            (
                                openai.error.ServiceUnavailableError,
                                openai.error.APIError,
                            ),
                        ):
                            logging.warning(
                                ERROR_ERRORS_TO_MESSAGES[type(e)].format(e=e)
                            )
                        elif isinstance(e, openai.error.InvalidRequestError):
                            logging.warning(ERROR_ERRORS_TO_MESSAGES[type(e)])
                            return {
                                "choices": [
                                    {
                                        "message": {
                                            "content": "Invalid Request: Prompt was filtered"  # noqa E501
                                        }
                                    }
                                ]
                            }
                        else:
                            logging.warning(ERROR_ERRORS_TO_MESSAGES[type(e)])
                        await asyncio.sleep(10)
                return {"choices": [{"message": {"content": ""}}]}

        async_responses = [
            _throttled_completion_acreate(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "user", "content": f"{prompt}"},
                ],
                temperature=temperature,
                max_tokens=500,
                n=responses_per_request,
                top_p=1,
                limiter=limiter,
            )
            for prompt in prompts
        ]
        responses = await tqdm_asyncio.gather(*async_responses)
        # Note: will never be none because it's set, but mypy doesn't know that.
        await openai.aiosession.get().close()
        return responses


def handle_api_error(e, api_call_counter):
    """Handle OpenAI errors or related errors that the API may raise.

    Args:
        e: The error to handle. This could be an OpenAI error or a related
           non-fatal error, such as JSONDecodeError or AssertionError.
        api_call_counter: The number of API calls made so far.

    Returns:
        The api_call_counter (if no error was raised), else raise the error.
    """
    logging.error(e)
    if isinstance(
        e,
        (openai.error.APIError, openai.error.Timeout, openai.error.RateLimitError),
    ):
        # For these errors, OpenAI recommends waiting before retrying.
        time.sleep(1)

    if isinstance(e, API_ERRORS):
        # For these errors, we can increment a counter and retry the API call.
        return api_call_counter
    else:
        # For all other errors, immediately throw an exception.
        raise e


def count_tokens_from_string(string: str, encoding_name: str = "cl100k_base") -> int:
    """Handle count the tokens in a string with OpenAI's tokenizer.

    Args:
        string: The string to count.
        encoding_name: The name of the tokenizer to use.

    Returns:
        The number of tokens in the string.
    """
    encoding = tiktoken.get_encoding(encoding_name)
    num_tokens = len(encoding.encode(string))
    return num_tokens