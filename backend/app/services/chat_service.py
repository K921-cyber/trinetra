"""
TRINETRA — AI Chatbot Service

Wrapper around OpenRouter's Free Models Router — a zero-cost inference
endpoint that automatically selects from currently available free
models. Acts as an in-app SOC-analyst assistant: explains the
dashboard, and when the frontend passes along current scan/target data
as `context`, interprets findings and generates structured
investigation reports.

Provider is configurable via OPENROUTER_API_KEY / OPENROUTER_MODEL /
OPENROUTER_BASE_URL. "openrouter/free" is a router, not a pinned
model — OpenRouter picks whichever free model is currently available
behind it, so this keeps working with zero code changes even as the
underlying free model rotates.
"""

import asyncio
import logging
import random
import re
import httpx
from app.core.config import settings

logger = logging.getLogger("trinetra.chat")

# Plain chat replies should stay quick and short. Report generation
# reads a large context block and writes a long, structured,
# table-heavy response — that needs a much bigger time and token
# budget than a normal chat turn.
CHAT_TIMEOUT_SECONDS = 60
REPORT_TIMEOUT_SECONDS = 180
CHAT_MAX_TOKENS = 1024
REPORT_MAX_TOKENS = 8192

# Bounded retries with exponential backoff for TRANSIENT errors only.
#   429 (rate limited), 500/502/503/504 (server-side / model temporarily
#   unavailable behind the free router) -> transient, worth retrying
#   400/401/403/404                     -> permanent, retrying is pointless
MAX_ATTEMPTS = 3
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
BASE_BACKOFF_SECONDS = 1.5

# Hard safety ceiling only — NOT a normal-path truncation. This cap only
# guards against a genuinely pathological payload and is logged loudly
# if it's ever actually hit, since that would mean real investigation
# data is being cut — which we do NOT want to do silently.
MAX_CONTEXT_CHARS = 150_000

# Defense-in-depth: some free models leak tool-call syntax as plain text
# even with no tools defined in the request and a system prompt telling
# them not to. Strip it server-side so it never reaches the UI, rather
# than relying on the prompt alone.
_TOOL_CALL_LEAK_PATTERN = re.compile(
    r"<\|?tool_call[_\s]?(start|end)?\|?>.*?(?=<\|?tool_call[_\s]?end\|?>|\Z)",
    re.DOTALL | re.IGNORECASE,
)


def _strip_tool_call_leaks(text: str) -> str:
    return _TOOL_CALL_LEAK_PATTERN.sub("", text).strip()

# Some free models behind the OpenRouter router are tool/function-call
# fine-tuned and will occasionally emit tool-call-style tokens (e.g.
# "<|tool_call_start|> [some_function(...)] <|tool_call_end|>") even
# though no tools were declared in the request. We never declare tools,
# so any such output is a hallucination, not a real answer — detect it
# and retry rather than showing it to the user as the report.
TOOL_CALL_HALLUCINATION_PATTERN = re.compile(
    r"<\|?\s*tool_call|^\s*\[?\s*[A-Za-z_][A-Za-z0-9_]*\s*\(.*\)\s*\]?\s*$",
    re.IGNORECASE | re.DOTALL,
)

# Some reasoning-capable free models leak their internal chain-of-thought
# into the visible content instead of (or before) the final answer, even
# with reasoning excluded. This is a cheap heuristic: real reports open
# with a "## Executive Summary" heading; leaked reasoning opens with
# first-person deliberation about the instructions instead.
REASONING_LEAK_PATTERN = re.compile(
    r"^\s*(wait,|let'?s\s|i'?ll\s|wait\s|actually,\s|i need to\s|the (instruction|user prompt|data) says)",
    re.IGNORECASE,
)


def _looks_like_bad_output(text: str, is_report_request: bool) -> bool:
    if not text:
        return True
    stripped = text.strip()
    if TOOL_CALL_HALLUCINATION_PATTERN.search(stripped):
        return True
    if is_report_request and REASONING_LEAK_PATTERN.match(stripped):
        return True
    return False

BASE_SYSTEM_PROMPT = (
    "You are the TRINETRA Assistant, an in-app SOC-analyst-style helper for the "
    "TRINETRA OSINT Dashboard. TRINETRA lets users search a target (domain, email, "
    "IP, username, phone) and runs OSINT plugins (infrastructure, threat, advanced "
    "categories) against it, shows results on a map/graph, and supports 'Watches' "
    "that re-check a target periodically and alert on changes. For everyday "
    "questions about the dashboard, keep replies brief and to the point (a few "
    "sentences) — only go long when the user is asking for a full investigation "
    "report. Be friendly and practical. If asked something unrelated, answer "
    "briefly and steer back to being useful.\n\n"
    "You do NOT have access to any tools, functions, or plugins yourself — you "
    "cannot call ANY function, by any name (e.g. 'trinetra_report' or similar). "
    "You only ever respond with plain markdown text written directly to the user. "
    "Never output tool-call syntax, function-call syntax, JSON function "
    "invocations, or special tokens such as <|tool_call_start|>, <tool_call>, "
    "[TOOL_CALL], or similar markers from any model family. If the user asks for "
    "a report, write it yourself, right now, as plain markdown text in your "
    "reply — never as a function call, never as JSON, and never wrapped in a "
    "single outer code fence around the whole response."
)

REPORT_INSTRUCTIONS = (
    "\n\nThe user's current investigation data is provided below (live OSINT scan "
    "results they are looking at right now). Use it to answer questions about the "
    "target, and if the user asks for a report, summary, or analysis, produce a "
    "comprehensive, professional SOC-analyst investigation report in markdown — "
    "written out directly as your reply, not as a tool/function call of any kind.\n\n"
    "STRUCTURE (use exactly these headings, in this order, as '## Heading', and "
    "only include a section if the data actually supports it):\n"
    "## Executive Summary\n"
    "## Target Overview\n"
    "## Key Findings\n"
    "## Risk Assessment\n"
    "## Recommended Actions\n\n"
    "'Executive Summary' must be 2-4 sentences only: overall risk level, the single "
    "biggest concern found (if any), and a one-line bottom-line verdict. No bullets, "
    "no headings inside it — plain prose only.\n\n"
    "Under 'Key Findings', add one '### ' subsection PER PLUGIN CATEGORY that has data "
    "(e.g. '### Infrastructure Analysis', '### Threat Analysis', '### Advanced / OSINT Findings'), "
    "and inside each subsection cover EVERY plugin result present in the data below in full "
    "detail — do not summarize away specific values. Whenever present in the data, always include: "
    "full domain/WHOIS records (registrar, creation/update/expiry dates, name servers), all resolved "
    "IP addresses and ASN/hosting info, every subdomain found (not just a sample) with its discovery "
    "SOURCE listed, DNS records (MX, SPF, DKIM, TXT), SSL/TLS certificate details, open ports and "
    "services, security headers, breach/leak counts and sources, CVEs, and any risk/exposure scores. "
    "When stating a count (e.g. 'N headers missing'), always name ALL N items — never trail off with "
    "'etc.' or 'and others'. Use markdown tables for lists of records (subdomains, ports, DNS records) "
    "instead of prose where possible. Base every claim only on the data given — do not invent findings "
    "that aren't present. If a plugin reported no data or an error, say so briefly instead of omitting it.\n\n"
    "=== CURRENT INVESTIGATION DATA ===\n{context}\n=== END DATA ==="
)


class ChatService:
    async def get_reply(
        self,
        message: str,
        history: list[dict] | None = None,
        context: str | None = None,
    ) -> str:
        if not settings.openrouter_api_key:
            return (
                "Chatbot isn't configured yet — set OPENROUTER_API_KEY in your "
                "backend .env file to enable AI responses. Get a free key at "
                "https://openrouter.ai/keys"
            )

        is_report_request = bool(context)

        system_prompt = BASE_SYSTEM_PROMPT
        if context:
            if len(context) > MAX_CONTEXT_CHARS:
                logger.warning(
                    "Investigation context (%d chars) exceeds safety ceiling "
                    "(%d chars) — trimming. This should be rare; if it happens "
                    "often, raise MAX_CONTEXT_CHARS.",
                    len(context), MAX_CONTEXT_CHARS,
                )
                context = context[:MAX_CONTEXT_CHARS]
            system_prompt += REPORT_INSTRUCTIONS.format(context=context)

        # OpenRouter's chat/completions endpoint is OpenAI-format: the
        # system prompt is a regular message with role "system".
        messages = [{"role": "system", "content": system_prompt}]
        for turn in (history or [])[-10:]:
            role = "user" if turn.get("role") == "user" else "assistant"
            messages.append({"role": role, "content": turn.get("content", "")})
        messages.append({"role": "user", "content": message})

        payload = {
            "model": settings.openrouter_model,
            "messages": messages,
            "max_tokens": REPORT_MAX_TOKENS if is_report_request else CHAT_MAX_TOKENS,
            # If the free router picks a reasoning-capable model, this tells
            # OpenRouter to run reasoning internally but exclude the reasoning
            # tokens from the visible response — otherwise a reasoning model
            # can burn its entire max_tokens budget "thinking out loud" and
            # never reach the actual answer, which is slow AND truncated.
            "reasoning": {"exclude": True},
        }
        headers = {
            "Authorization": f"Bearer {settings.openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": settings.frontend_url,
            "X-Title": "TRINETRA OSINT Dashboard",
        }
        url = f"{settings.openrouter_base_url.rstrip('/')}/chat/completions"
        timeout = REPORT_TIMEOUT_SECONDS if is_report_request else CHAT_TIMEOUT_SECONDS

        last_error: Exception | None = None
        last_hallucination = False

        for attempt in range(MAX_ATTEMPTS):
            is_last_attempt = attempt == MAX_ATTEMPTS - 1

            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(url, headers=headers, json=payload)
                    resp.raise_for_status()
                    data = resp.json()

                    # OpenRouter can return HTTP 200 with an "error" body if the
                    # free model it picked failed internally — treat as transient.
                    if "error" in data:
                        err_msg = data["error"].get("message", "unknown error")
                        logger.warning(
                            "OpenRouter returned an in-body error (attempt %d/%d): %s",
                            attempt + 1, MAX_ATTEMPTS, err_msg,
                        )
                        last_error = RuntimeError(err_msg)
                        if is_last_attempt:
                            break
                        await asyncio.sleep(BASE_BACKOFF_SECONDS * (2 ** attempt) + random.uniform(0, 0.5))
                        continue

                    choices = data.get("choices", [])
                    if not choices:
                        logger.warning("OpenRouter returned no choices: %s", str(data)[:300])
                        return "Sorry, I didn't get a response — try again."
                     # .get("content", "") only falls back when the key is
                    # missing entirely. Some models return an *explicit*
                    # "content": null (e.g. when reasoning is excluded and
                    # the model spent its whole budget on reasoning, leaving
                    # nothing for the actual answer) — `or ""` catches that
                    # case too so text is always a string, never None.
                    text = choices[0].get("message", {}).get("content") or ""
                    text = _strip_tool_call_leaks(text)
                    return text or "Sorry, I didn't get a response — try again."

                    # Some free models hallucinate a fake tool call, or leak
                    # their internal reasoning, instead of writing the actual
                    # report — even though we never declared tools and asked
                    # for reasoning to be excluded. Treat either as a
                    # transient failure and retry rather than showing it.
                    if _looks_like_bad_output(text, is_report_request):
                        logger.warning(
                            "OpenRouter model emitted tool-call-style output instead "
                            "of a text reply (attempt %d/%d): %r",
                            attempt + 1, MAX_ATTEMPTS, text[:200],
                        )
                        last_error = RuntimeError("model emitted tool-call syntax instead of a text reply")
                        last_hallucination = True
                        if is_last_attempt:
                            break
                        await asyncio.sleep(BASE_BACKOFF_SECONDS * (2 ** attempt) + random.uniform(0, 0.5))
                        continue

                    return text

            except httpx.TimeoutException as e:
                last_error = e
                logger.warning(
                    "OpenRouter request timed out (attempt %d/%d, timeout=%ds, report=%s)",
                    attempt + 1, MAX_ATTEMPTS, timeout, is_report_request,
                )
                if is_last_attempt:
                    break
                await asyncio.sleep(BASE_BACKOFF_SECONDS * (2 ** attempt) + random.uniform(0, 0.5))
                continue

            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                logger.error(
                    "OpenRouter HTTP error %s (attempt %d/%d): %s",
                    status, attempt + 1, MAX_ATTEMPTS, e.response.text[:300],
                )

                if status in TRANSIENT_STATUS_CODES and not is_last_attempt:
                    last_error = e
                    retry_after = e.response.headers.get("retry-after")
                    if retry_after:
                        try:
                            wait = min(float(retry_after), 10.0)
                        except ValueError:
                            wait = BASE_BACKOFF_SECONDS * (2 ** attempt)
                    else:
                        wait = BASE_BACKOFF_SECONDS * (2 ** attempt) + random.uniform(0, 0.5)
                    await asyncio.sleep(wait)
                    continue

                # Permanent error, OR a transient one that's exhausted its retries.
                # Every branch below returns a clean, user-friendly message only —
                # raw provider error text is logged above, never shown in the UI.
                if status in (401, 403):
                    return "Chatbot is misconfigured — check that OPENROUTER_API_KEY is valid."
                if status == 429:
                    return "The free AI tier is rate-limited right now — please wait a moment and try again."
                if is_report_request:
                    return (
                        "Generating a full report is taking longer than expected, or the "
                        "currently available free model is briefly unavailable. Please try "
                        "again; it usually completes on the next attempt."
                    )
                return "Chatbot service is temporarily unavailable. Please try again shortly."

            except Exception as e:
                logger.error("OpenRouter request failed: %s", e, exc_info=True)
                return "Chatbot service is temporarily unavailable. Please try again shortly."

        logger.error("OpenRouter request exhausted all %d attempts: %s", MAX_ATTEMPTS, last_error)
        if last_hallucination:
            return (
                "The free AI model that responded this time tried to use a tool that "
                "doesn't exist instead of writing the report. This is a known quirk of "
                "some free models on OpenRouter's router — please try again; a different "
                "model is often picked on retry."
            )
        if is_report_request:
            return (
                "Generating a full report is taking longer than expected — this can "
                "happen with the free AI tier under load. Please try again; it "
                "usually completes on the next attempt."
            )
        return "Chatbot service is temporarily unavailable. Please try again shortly."


chat_service = ChatService()